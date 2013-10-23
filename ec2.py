#!/usr/bin/env python

# (c) 2013, Sam Lucidi
# Portions (c) 2012, Peter Sankauskas

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

######################################################################

import sys
import os
import argparse
import re
from time import time
import boto
from boto import ec2
from boto import rds
import ConfigParser
from collections import defaultdict

try:
    import json
except ImportError:
    import simplejson as json

try:
    ec2_default_ini_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'ec2.ini')
    ec2_ini_path = os.environ.get('EC2_INI_PATH', ec2_default_ini_path)
    config = ConfigParser.SafeConfigParser()
    config.read(ec2_ini_path)
except:
    print("Couldn't find an ec2.ini file, using defaults.")

# sensible defaults
if config.has_option('ec2', 'cache_max_age'):
    CACHE_MAX_AGE = config.getint('ec2', 'cache_max_age')
else:
    CACHE_MAX_AGE = 300
if config.has_option('ec2', 'cache_base_path'):
    CACHE_BASE_PATH = config.get('ec2', 'cache_base_path')
else:
    CACHE_BASE_PATH = '/tmp'

CACHE_FILE_PATH = os.path.join(CACHE_BASE_PATH, "ansible-ec2.cache")
CACHE_INDEX_PATH = os.path.join(CACHE_BASE_PATH, "ansible-ec2.index")

# is eucalyptus?
EUCALYPTUS_HOST = None
EUCALYPTUS = False
if config.has_option('ec2', 'eucalyptus'):
    EUCALYPTUS = config.getboolean('ec2', 'eucalyptus')
if EUCALYPTUS and config.has_option('ec2', 'eucalyptus_host'):
    EUCALYPTUS_HOST = config.get('ec2', 'eucalyptus_host')

# AWS regions to inventory
REGIONS = []
if config.has_option('ec2', 'regions'):
    config_regions = config.get('ec2', 'regions').strip()
else:
    config_regions = 'all'

if (config_regions == 'all'):
    if EUCALYPTUS_HOST:
        REGIONS.append(boto.connect_euca(host=EUCALYPTUS_HOST).region.name)
    else:
        for region_info in ec2.regions():
            REGIONS.append(region_info.name)
else:
    REGIONS = re.split(", |,|\s", config_regions)

# The name of the ec2 instance variable containing the address that Ansible
# should use to SSH into that instance. Generally you want to use the
# public_dns_name for instances out in the main cloud. Since VPC instances
# generally don't *have* a public_dns_name, use the private_dns_name.
HOST_ADDRESS_VARIABLE = config.get('ec2', 'host_address_variable', 'public_dns_name')
VPC_HOST_ADDRESS_VARIABLE = config.get('ec2', 'vpc_host_address_variable', 'private_dns_name')

# Whether to use Route53 to get host names for instances
# from your zones. If this is enabled, you'll be able 
# to address instances by their hostname.
if config.has_option('ec2', 'use_route53'):
    USE_ROUTE53 = config.getboolean('ec2', 'use_route53')
else:
    USE_ROUTE53 = False

# Inventory grouped by instance IDs, tags, security groups, regions,
# and availability zones
inventory = {}
# Index of hostname (address) to instance ID
index = {}

def get_instances_by_region(region, inventory, index, records=None):
    """ 
    Makes an AWS EC2 API call to the list of instances in a particular
    region 

    """

    if EUCALYPTUS:
        conn = boto.connect_euca(host=EUCALYPTUS_HOST)
        conn.APIVersion = '2010-08-31'
    else:
        conn = ec2.connect_to_region(region)

    # connect_to_region will fail "silently" by returning None if the region name is wrong or not supported
    if conn is None:
        print("Region name: %s likely not supported, or AWS is down.  Connection to region failed." % region)
        sys.exit(1)

    reservations = conn.get_all_instances()
    for reservation in reservations:
        for instance in reservation.instances:
            add_instance(instance, region, inventory, index)

def get_rds_instances_by_region(region, inventory, index, records=None):
    """
    Makes an AWS API call to the list of RDS instances in a particular
    region

    """

    try:
        conn = rds.connect_to_region(region)
        if conn:
            instances = conn.get_all_dbinstances()
            for instance in instances:
                add_rds_instance(instance, region, inventory, index, records)

    except boto.exception.BotoServerError as e:
        print "Looks like AWS RDS is down: "
        print e
        sys.exit(1)

def get_instance(region, instance_id):
    """
    Get details about a specific instance

    """

    if EUCALYPTUS:
        conn = boto.connect_euca(EUCALYPTUS_HOST)
        conn.APIVersion = '2010-08-31'
    else:
        conn = ec2.connect_to_region(region)

    # connect_to_region will fail "silently" by returning None if the region name is wrong or not supported
    if conn is None:
        print("region name: %s likely not supported, or AWS is down.  connection to region failed." % region)
        sys.exit(1)

    reservations = conn.get_all_instances([instance_id])
    for reservation in reservations:
        for instance in reservation.instances:
            return instance


def add_instance(instance, region, inventory, index, records=None):
    """
    Adds an instance to the inventory and index, as long as it is
    addressable

    """

    # Only want running instances
    if instance.state != 'running':
        return

    # Select the best destination address
    if instance.subnet_id:
        dest = getattr(instance, VPC_HOST_ADDRESS_VARIABLE)
    else:
        dest =  getattr(instance, HOST_ADDRESS_VARIABLE)

    if not dest:
        # Skip instances we cannot address (e.g. private VPC subnet)
        return

    # Add to index
    index[dest] = [region, instance.id]

    # Inventory: Group by instance ID (always a group of 1)
    inventory[instance.id] = [dest]

    # Inventory: Group by region
    inventory[region].append(dest)
    # Inventory: Group by availability zone
    inventory[instance.placement].append(dest)
    # Inventory: Group by instance type
    inventory[to_safe('type_' + instance.instance_type)].append(dest)

    # Inventory: Group by key pair
    if instance.key_name:
        key_name = to_safe('key_' + instance.key_name)
        inventory[key_name].append(dest)

    # Inventory: Group by security group
    try:
        for group in instance.groups:
            key = to_safe("security_group_" + group.name)
            inventory[key].append(dest)
    except AttributeError:
        print("Using older version of Boto, won't be able to inventory by security group.")
        print("Please upgrade to a version of boto >= 2.3.0")

    # Inventory: Group by tag keys
    for k, v in instance.tags.iteritems():
        key = to_safe("tag_" + k + "=" + v)
        inventory[key].append(dest)

    # Inventory: Group by Route53 domain names if enabled
    if records:
        route53_names = get_instance_route53_names(instance, records)
        for name in route53_names:
            inventory[name].append(dest)

    if instance.instance_profile:
        profile_name = to_safe('profile_' + instance.instance_profile['arn'].split('/')[-1])
        inventory[profile_name].append(dest)

    return inventory, index

def add_rds_instance(instance, region, inventory, index, records=None):
    """
    Adds an RDS instance to the inventory and index, as long as it is
    addressable

    """

    # Only want available instances
    if instance.status != 'available':
        return

    dest = instance.endpoint[0]

    if not dest:
        # Skip instances we cannot address (e.g. private VPC subnet)
        return

    # Add to index
    index[dest] = [region, instance.id]

    # Inventory: Group by instance ID (always a group of 1)
    inventory[instance.id] = [dest]
    # Inventory: Group by region
    inventory[region].append(dest)
    # Inventory: Group by availability zone
    inventory[instance.availability_zone].append(dest)
    # Inventory: Group by instance type
    inventory[to_safe('type_' + instance.instance_class)].append(dest)

    # Inventory: Group by security group
    try:
        if instance.security_group:
            key = to_safe("security_group_" + instance.security_group.name)
            inventory[key].append(dest)
    except AttributeError:
        print("Using older version of boto, won't be able to inventory by security group.")
        print("Please upgrade to a version of boto >= 2.3.0")

    # Inventory: Group by engine
    inventory[to_safe("rds_" + instance.engine)].append(dest)
    # Inventory: Group by parameter group
    inventory[to_safe("rds_parameter_group_" + instance.parameter_group.name)].append(dest)

    return inventory, index

def get_route53_records():
    """
    Get and store the map of resource records to domain names that
    point to them.

    """

    r53 = boto.connect_route53()

    route53_zones = [zone for zone in r53.get_zones()]

    route53_records = {}

    for zone in route53_zones:
        rrsets = r53_conn.get_all_rrsets(zone.id)

        for record_set in rrsets:
            record_name = record_set.name

            if record_name.endswith('.'):
                record_name = record_name[:-1]

            for resource in record_set.resource_records:
                route53_records.setdefault(resource, set())
                route53_records[resource].add(record_name)

    return route53_records


def get_instance_route53_names(instance, route53_records):
    """
    Check if an instance is referenced in the records we have from
    Route53. If it is, return the list of domain names pointing to said
    instance. If nothing points to it, return an empty list.

    """

    instance_attributes = [ 'public_dns_name', 'private_dns_name',
                            'ip_address', 'private_ip_address' ]

    name_list = set()

    for attrib in instance_attributes:
        try:
            value = getattr(instance, attrib)
        except AttributeError:
            continue

        if value in route53_records:
            name_list.update(route53_records[value])

    return list(name_list)


def get_host_info(index, host):
    """ Get variables about a specific host """

    if len(index) == 0:
        # Need to load index from cache
        index = load_from_cache(CACHE_INDEX_PATH)

    if not host in index:
        if not host in index:
            return json.dumps({})

    (region, instance_id) = index[host]

    instance = get_instance(region, instance_id)
    instance_vars = {}
    for key in vars(instance):
        value = getattr(instance, key)
        key = to_safe('ec2_' + key)

        # Handle complex types
        if type(value) in [int, bool]:
            instance_vars[key] = value
        elif type(value) in [str, unicode]:
            instance_vars[key] = value.strip()
            if key == "ec2_private_ip_address":
                instance_vars['ec2_pretty_private_ip'] = value.replace('.', '-')
            if key == "ec2_public_ip_address":
                instance_vars['ec2_pretty_public_ip'] = value.replace('.', '-')
        elif type(value) == type(None):
            instance_vars[key] = ''
        elif key == 'ec2_region':
            instance_vars[key] = value.name
        elif key == 'ec2_tags':
            for k, v in value.iteritems():
                key = to_safe('ec2_tag_' + k)
                instance_vars[key] = v
        elif key == 'ec2_groups':
            group_ids = []
            group_names = []
            for group in value:
                group_ids.append(group.id)
                group_names.append(group.name)
            instance_vars["ec2_security_group_ids"] = ','.join(group_ids)
            instance_vars["ec2_security_group_names"] = ','.join(group_names)
        else:
            pass

    return json.dumps(instance_vars, sort_keys=True, indent=2)

def load_from_cache(filename):
    """ Reads the index from the cache file sets self.index """

    cache = open(filename, 'r')
    json_index = cache.read()
    cache.close()
    return json.loads(json_index)

def cache_expired():
    """
    Return whether the cache files have expired.

    """

    if os.path.isfile(CACHE_FILE_PATH):
        mod_time = os.path.getmtime(CACHE_FILE_PATH)
        current_time = time()
        if (mod_time + CACHE_MAX_AGE) > current_time:
            if os.path.isfile(CACHE_INDEX_PATH):
                return False

    return True

def to_safe(word):
    """
    Converts 'bad' characters in a string to underscores so they can be
    used as Ansible groups

    """

    return re.sub("[^A-Za-z0-9\-]", "_", word)

def update_inventory():
    """ Do API calls to each region, and save data in cache files """

    inventory = defaultdict(list)
    index = defaultdict(list)

    if USE_ROUTE53:
        records = get_route53_records()
    else:
        records = None

    for region in REGIONS:
        get_instances_by_region(region, inventory, index, records)
        get_rds_instances_by_region(region, inventory, index, records)

    with open(CACHE_FILE_PATH, 'w') as cache_file:
        inventory_data = json.dumps(inventory, sort_keys=True, indent=2)
        cache_file.write(inventory_data)

    with open(CACHE_INDEX_PATH, 'w') as index_file:
        index_data = json.dumps(index, sort_keys=True, indent=2)
        index_file.write(index_data)

    return inventory, index

# Run the script
if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on EC2')
    parser.add_argument('-l', '--list', action='store_true', default=True,
                       help='List instances (default: True)')
    parser.add_argument('--host', action='store',
                       help='Get all the variables about a specific instance')
    parser.add_argument('-r', '--refresh-cache', action='store_true', default=False,
                       help='Force refresh of cache by making API requests to EC2 (default: False - use cache files)')
    args = parser.parse_args()

    if cache_expired() or args.refresh_cache:
        inventory, index = update_inventory()
    else:
        with open(CACHE_FILE_PATH, 'r') as inv_file:
            inventory = json.load(inv_file)
        with open(CACHE_INDEX_PATH, 'r') as index_file:
            index = json.load(index_file)

    # Data to print
    if args.host:
        print(get_host_info(index, args.host))
    elif args.list:
        print(json.dumps(inventory, sort_keys=True, indent=2))
