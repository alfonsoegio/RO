# -*- coding: utf-8 -*-

##
# Copyright 2015 Telefonica Investigacion y Desarrollo, S.A.U.
# This file is part of openmano
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# For those usages not covered by the Apache License, Version 2.0 please
# contact with: nfvlabs@tid.es
##

'''
NFVO engine, implementing all the methods for the creation, deletion and management of vnfs, scenarios and instances
'''
__author__="Alfonso Tierno, Gerardo Garcia, Pablo Montes"
__date__ ="$16-sep-2014 22:05:01$"

# import imp
import json
import yaml
import utils
from utils import deprecated
import vim_thread
import console_proxy_thread as cli
import vimconn
import logging
import collections
import math
from uuid import uuid4
from db_base import db_base_Exception

import nfvo_db
from threading import Lock
import time as t
from lib_osm_openvim import ovim as ovim_module
from lib_osm_openvim.ovim import ovimException
from Crypto.PublicKey import RSA

import osm_im.vnfd as vnfd_catalog
import osm_im.nsd as nsd_catalog
from pyangbind.lib.serialise import pybindJSONDecoder
from copy import deepcopy


# WIM
import wim.wimconn as wimconn
import wim.wim_thread as wim_thread
from .http_tools import errors as httperrors
from .wim.engine import WimEngine
from .wim.persistence import WimPersistence
from copy import deepcopy
from pprint import pformat
#

global global_config
global vimconn_imported
# WIM
global wim_engine
wim_engine  = None
global wimconn_imported
#
global logger
global default_volume_size
default_volume_size = '5' #size in GB
global ovim
ovim = None
global_config = None

vimconn_imported = {}   # dictionary with VIM type as key, loaded module as value
vim_threads = {"running":{}, "deleting": {}, "names": []}      # threads running for attached-VIMs
vim_persistent_info = {}
# WIM
wimconn_imported = {}   # dictionary with WIM type as key, loaded module as value
wim_threads = {"running":{}, "deleting": {}, "names": []}      # threads running for attached-WIMs
wim_persistent_info = {}
#

logger = logging.getLogger('openmano.nfvo')
task_lock = Lock()
last_task_id = 0.0
db = None
db_lock = Lock()


class NfvoException(httperrors.HttpMappedError):
    """Common Class for NFVO errors"""


def get_task_id():
    global last_task_id
    task_id = t.time()
    if task_id <= last_task_id:
        task_id = last_task_id + 0.000001
    last_task_id = task_id
    return "ACTION-{:.6f}".format(task_id)
    # return (t.strftime("%Y%m%dT%H%M%S.{}%Z", t.localtime(task_id))).format(int((task_id % 1)*1e6))


def new_task(name, params, depends=None):
    """Deprected!!!"""
    task_id = get_task_id()
    task = {"status": "enqueued", "id": task_id, "name": name, "params": params}
    if depends:
        task["depends"] = depends
    return task


def is_task_id(id):
    return True if id[:5] == "TASK-" else False


def get_non_used_vim_name(datacenter_name, datacenter_id, tenant_name, tenant_id):
    name = datacenter_name[:16]
    if name not in vim_threads["names"]:
        vim_threads["names"].append(name)
        return name
    if tenant_name:
        name = datacenter_name[:16] + "." + tenant_name[:16]
        if name not in vim_threads["names"]:
            vim_threads["names"].append(name)
            return name
    name = datacenter_id
    vim_threads["names"].append(name)
    return name

# -- Move
def get_non_used_wim_name(wim_name, wim_id, tenant_name, tenant_id):
    name = wim_name[:16]
    if name not in wim_threads["names"]:
        wim_threads["names"].append(name)
        return name
    name = wim_name[:16] + "." + tenant_name[:16]
    if name not in wim_threads["names"]:
        wim_threads["names"].append(name)
        return name
    name = wim_id + "-" + tenant_id
    wim_threads["names"].append(name)
    return name


def start_service(mydb, persistence=None, wim=None):
    global db, global_config
    db = nfvo_db.nfvo_db(lock=db_lock)
    mydb.lock = db_lock
    db.connect(global_config['db_host'], global_config['db_user'], global_config['db_passwd'], global_config['db_name'])
    global ovim

    persistence = persistence or  WimPersistence(db)

    # Initialize openvim for SDN control
    # TODO: Avoid static configuration by adding new parameters to openmanod.cfg
    # TODO: review ovim.py to delete not needed configuration
    ovim_configuration = {
        'logger_name': 'openmano.ovim',
        'network_vlan_range_start': 1000,
        'network_vlan_range_end': 4096,
        'db_name': global_config["db_ovim_name"],
        'db_host': global_config["db_ovim_host"],
        'db_user': global_config["db_ovim_user"],
        'db_passwd': global_config["db_ovim_passwd"],
        'bridge_ifaces': {},
        'mode': 'normal',
        'network_type': 'bridge',
        #TODO: log_level_of should not be needed. To be modified in ovim
        'log_level_of': 'DEBUG'
    }
    try:
        # starts ovim library
        ovim = ovim_module.ovim(ovim_configuration)

        global wim_engine
        wim_engine = wim or WimEngine(persistence)
        wim_engine.ovim = ovim

        ovim.start_service()

        #delete old unneeded vim_wim_actions
        clean_db(mydb)

        # starts vim_threads
        from_= 'tenants_datacenters as td join datacenters as d on td.datacenter_id=d.uuid join '\
                'datacenter_tenants as dt on td.datacenter_tenant_id=dt.uuid'
        select_ = ('type', 'd.config as config', 'd.uuid as datacenter_id', 'vim_url', 'vim_url_admin',
                   'd.name as datacenter_name', 'dt.uuid as datacenter_tenant_id',
                   'dt.vim_tenant_name as vim_tenant_name', 'dt.vim_tenant_id as vim_tenant_id',
                   'user', 'passwd', 'dt.config as dt_config', 'nfvo_tenant_id')
        vims = mydb.get_rows(FROM=from_, SELECT=select_)
        for vim in vims:
            extra={'datacenter_tenant_id': vim.get('datacenter_tenant_id'),
                   'datacenter_id': vim.get('datacenter_id')}
            if vim["config"]:
                extra.update(yaml.load(vim["config"]))
            if vim.get('dt_config'):
                extra.update(yaml.load(vim["dt_config"]))
            if vim["type"] not in vimconn_imported:
                module_info=None
                try:
                    module = "vimconn_" + vim["type"]
                    pkg = __import__("osm_ro." + module)
                    vim_conn = getattr(pkg, module)
                    # module_info = imp.find_module(module, [__file__[:__file__.rfind("/")]])
                    # vim_conn = imp.load_module(vim["type"], *module_info)
                    vimconn_imported[vim["type"]] = vim_conn
                except (IOError, ImportError) as e:
                    # if module_info and module_info[0]:
                    #    file.close(module_info[0])
                    raise NfvoException("Unknown vim type '{}'. Cannot open file '{}.py'; {}: {}".format(
                        vim["type"], module, type(e).__name__, str(e)), httperrors.Bad_Request)

            thread_id = vim['datacenter_tenant_id']
            vim_persistent_info[thread_id] = {}
            try:
                #if not tenant:
                #    return -httperrors.Bad_Request, "You must provide a valid tenant name or uuid for VIM  %s" % ( vim["type"])
                myvim = vimconn_imported[ vim["type"] ].vimconnector(
                    uuid=vim['datacenter_id'], name=vim['datacenter_name'],
                    tenant_id=vim['vim_tenant_id'], tenant_name=vim['vim_tenant_name'],
                    url=vim['vim_url'], url_admin=vim['vim_url_admin'],
                    user=vim['user'], passwd=vim['passwd'],
                    config=extra, persistent_info=vim_persistent_info[thread_id]
                )
            except vimconn.vimconnException as e:
                myvim = e
                logger.error("Cannot launch thread for VIM {} '{}': {}".format(vim['datacenter_name'],
                                                                               vim['datacenter_id'], e))
            except Exception as e:
                raise NfvoException("Error at VIM  {}; {}: {}".format(vim["type"], type(e).__name__, e),
                                    httperrors.Internal_Server_Error)
            thread_name = get_non_used_vim_name(vim['datacenter_name'], vim['datacenter_id'], vim['vim_tenant_name'],
                                                vim['vim_tenant_id'])
            new_thread = vim_thread.vim_thread(task_lock, thread_name, vim['datacenter_name'],
                                               vim['datacenter_tenant_id'], db=db, db_lock=db_lock, ovim=ovim)
            new_thread.start()
            vim_threads["running"][thread_id] = new_thread

        wim_engine.start_threads()
    except db_base_Exception as e:
        raise NfvoException(str(e) + " at nfvo.get_vim", e.http_code)
    except ovim_module.ovimException as e:
        message = str(e)
        if message[:22] == "DATABASE wrong version":
            message = "DATABASE wrong version of lib_osm_openvim {msg} -d{dbname} -u{dbuser} -p{dbpass} {ver}' "\
                      "at host {dbhost}".format(
                            msg=message[22:-3], dbname=global_config["db_ovim_name"],
                            dbuser=global_config["db_ovim_user"], dbpass=global_config["db_ovim_passwd"],
                            ver=message[-3:-1], dbhost=global_config["db_ovim_host"])
        raise NfvoException(message, httperrors.Bad_Request)


def stop_service():
    global ovim, global_config
    if ovim:
        ovim.stop_service()
    for thread_id, thread in vim_threads["running"].items():
        thread.insert_task("exit")
        vim_threads["deleting"][thread_id] = thread
    vim_threads["running"] = {}

    if wim_engine:
        wim_engine.stop_threads()

    if global_config and global_config.get("console_thread"):
        for thread in global_config["console_thread"]:
            thread.terminate = True

def get_version():
    return  ("openmanod version {} {}\n(c) Copyright Telefonica".format(global_config["version"],
                                                                        global_config["version_date"] ))

def clean_db(mydb):
    """
    Clean unused or old entries at database to avoid unlimited growing
    :param mydb: database connector
    :return: None
    """
    # get and delete unused vim_wim_actions: all elements deleted, one week before, instance not present
    now = t.time()-3600*24*7
    instance_action_id = None
    nb_deleted = 0
    while True:
        actions_to_delete = mydb.get_rows(
            SELECT=("item", "item_id", "instance_action_id"),
            FROM="vim_wim_actions as va join instance_actions as ia on va.instance_action_id=ia.uuid "
                    "left join instance_scenarios as i on ia.instance_id=i.uuid",
            WHERE={"va.action": "DELETE", "va.modified_at<": now, "i.uuid": None,
                   "va.status": ("DONE", "SUPERSEDED")},
            LIMIT=100
        )
        for to_delete in actions_to_delete:
            mydb.delete_row(FROM="vim_wim_actions", WHERE=to_delete)
            if instance_action_id != to_delete["instance_action_id"]:
                instance_action_id = to_delete["instance_action_id"]
                mydb.delete_row(FROM="instance_actions", WHERE={"uuid": instance_action_id})
        nb_deleted += len(actions_to_delete)
        if len(actions_to_delete) < 100:
            break
    # clean locks
    mydb.update_rows("vim_wim_actions", UPDATE={"worker": None}, WHERE={"worker<>": None})

    if nb_deleted:
        logger.debug("Removed {} unused vim_wim_actions".format(nb_deleted))


def get_flavorlist(mydb, vnf_id, nfvo_tenant=None):
    '''Obtain flavorList
    return result, content:
        <0, error_text upon error
        nb_records, flavor_list on success
    '''
    WHERE_dict={}
    WHERE_dict['vnf_id'] = vnf_id
    if nfvo_tenant is not None:
        WHERE_dict['nfvo_tenant_id'] = nfvo_tenant

    #result, content = mydb.get_table(FROM='vms join vnfs on vms.vnf_id = vnfs.uuid',SELECT=('uuid'),WHERE=WHERE_dict )
    #result, content = mydb.get_table(FROM='vms',SELECT=('vim_flavor_id',),WHERE=WHERE_dict )
    flavors = mydb.get_rows(FROM='vms join flavors on vms.flavor_id=flavors.uuid',SELECT=('flavor_id',),WHERE=WHERE_dict )
    #print "get_flavor_list result:", result
    #print "get_flavor_list content:", content
    flavorList=[]
    for flavor in flavors:
        flavorList.append(flavor['flavor_id'])
    return flavorList


def get_imagelist(mydb, vnf_id, nfvo_tenant=None):
    """
    Get used images of all vms belonging to this VNFD
    :param mydb: database conector
    :param vnf_id: vnfd uuid
    :param nfvo_tenant: tenant, not used
    :return: The list of image uuid used
    """
    image_list = []
    vms = mydb.get_rows(SELECT=('image_id','image_list'), FROM='vms', WHERE={'vnf_id': vnf_id})
    for vm in vms:
        if vm["image_id"] and vm["image_id"] not in image_list:
            image_list.append(vm["image_id"])
        if vm["image_list"]:
            vm_image_list = yaml.load(vm["image_list"])
            for image_dict in vm_image_list:
                if image_dict["image_id"] not in image_list:
                    image_list.append(image_dict["image_id"])
    return image_list


def get_vim(mydb, nfvo_tenant=None, datacenter_id=None, datacenter_name=None, datacenter_tenant_id=None,
            vim_tenant=None, vim_tenant_name=None, vim_user=None, vim_passwd=None, ignore_errors=False):
    '''Obtain a dictionary of VIM (datacenter) classes with some of the input parameters
    return dictionary with {datacenter_id: vim_class, ... }. vim_class contain:
            'nfvo_tenant_id','datacenter_id','vim_tenant_id','vim_url','vim_url_admin','datacenter_name','type','user','passwd'
        raise exception upon error
    '''
    WHERE_dict={}
    if nfvo_tenant     is not None:  WHERE_dict['nfvo_tenant_id'] = nfvo_tenant
    if datacenter_id   is not None:  WHERE_dict['d.uuid']  = datacenter_id
    if datacenter_tenant_id is not None:  WHERE_dict['datacenter_tenant_id']  = datacenter_tenant_id
    if datacenter_name is not None:  WHERE_dict['d.name']  = datacenter_name
    if vim_tenant      is not None:  WHERE_dict['dt.vim_tenant_id']  = vim_tenant
    if vim_tenant_name is not None:  WHERE_dict['vim_tenant_name']  = vim_tenant_name
    if nfvo_tenant or vim_tenant or vim_tenant_name or datacenter_tenant_id:
        from_= 'tenants_datacenters as td join datacenters as d on td.datacenter_id=d.uuid join datacenter_tenants as dt on td.datacenter_tenant_id=dt.uuid'
        select_ = ('type','d.config as config','d.uuid as datacenter_id', 'vim_url', 'vim_url_admin', 'd.name as datacenter_name',
                   'dt.uuid as datacenter_tenant_id','dt.vim_tenant_name as vim_tenant_name','dt.vim_tenant_id as vim_tenant_id',
                   'user','passwd', 'dt.config as dt_config')
    else:
        from_ = 'datacenters as d'
        select_ = ('type','config','d.uuid as datacenter_id', 'vim_url', 'vim_url_admin', 'd.name as datacenter_name')
    try:
        vims = mydb.get_rows(FROM=from_, SELECT=select_, WHERE=WHERE_dict )
        vim_dict={}
        for vim in vims:
            extra={'datacenter_tenant_id': vim.get('datacenter_tenant_id'),
                   'datacenter_id': vim.get('datacenter_id'),
                   '_vim_type_internal': vim.get('type')}
            if vim["config"]:
                extra.update(yaml.load(vim["config"]))
            if vim.get('dt_config'):
                extra.update(yaml.load(vim["dt_config"]))
            if vim["type"] not in vimconn_imported:
                module_info=None
                try:
                    module = "vimconn_" + vim["type"]
                    pkg = __import__("osm_ro." + module)
                    vim_conn = getattr(pkg, module)
                    # module_info = imp.find_module(module, [__file__[:__file__.rfind("/")]])
                    # vim_conn = imp.load_module(vim["type"], *module_info)
                    vimconn_imported[vim["type"]] = vim_conn
                except (IOError, ImportError) as e:
                    # if module_info and module_info[0]:
                    #     file.close(module_info[0])
                    if ignore_errors:
                        logger.error("Unknown vim type '{}'. Can not open file '{}.py'; {}: {}".format(
                                            vim["type"], module, type(e).__name__, str(e)))
                        continue
                    raise NfvoException("Unknown vim type '{}'. Can not open file '{}.py'; {}: {}".format(
                                            vim["type"], module, type(e).__name__, str(e)), httperrors.Bad_Request)

            try:
                if 'datacenter_tenant_id' in vim:
                    thread_id = vim["datacenter_tenant_id"]
                    if thread_id not in vim_persistent_info:
                        vim_persistent_info[thread_id] = {}
                    persistent_info = vim_persistent_info[thread_id]
                else:
                    persistent_info = {}
                #if not tenant:
                #    return -httperrors.Bad_Request, "You must provide a valid tenant name or uuid for VIM  %s" % ( vim["type"])
                vim_dict[ vim['datacenter_id'] ] = vimconn_imported[ vim["type"] ].vimconnector(
                                uuid=vim['datacenter_id'], name=vim['datacenter_name'],
                                tenant_id=vim.get('vim_tenant_id',vim_tenant),
                                tenant_name=vim.get('vim_tenant_name',vim_tenant_name),
                                url=vim['vim_url'], url_admin=vim['vim_url_admin'],
                                user=vim.get('user',vim_user), passwd=vim.get('passwd',vim_passwd),
                                config=extra, persistent_info=persistent_info
                        )
            except Exception as e:
                if ignore_errors:
                    logger.error("Error at VIM  {}; {}: {}".format(vim["type"], type(e).__name__, str(e)))
                    continue
                http_code = httperrors.Internal_Server_Error
                if isinstance(e, vimconn.vimconnException):
                    http_code = e.http_code
                raise NfvoException("Error at VIM  {}; {}: {}".format(vim["type"], type(e).__name__, str(e)), http_code)
        return vim_dict
    except db_base_Exception as e:
        raise NfvoException(str(e) + " at nfvo.get_vim", e.http_code)


def rollback(mydb,  vims, rollback_list):
    undeleted_items=[]
    #delete things by reverse order
    for i in range(len(rollback_list)-1, -1, -1):
        item = rollback_list[i]
        if item["where"]=="vim":
            if item["vim_id"] not in vims:
                continue
            if is_task_id(item["uuid"]):
                continue
            vim = vims[item["vim_id"]]
            try:
                if item["what"]=="image":
                    vim.delete_image(item["uuid"])
                    mydb.delete_row(FROM="datacenters_images", WHERE={"datacenter_vim_id": vim["id"], "vim_id":item["uuid"]})
                elif item["what"]=="flavor":
                    vim.delete_flavor(item["uuid"])
                    mydb.delete_row(FROM="datacenters_flavors", WHERE={"datacenter_vim_id": vim["id"], "vim_id":item["uuid"]})
                elif item["what"]=="network":
                    vim.delete_network(item["uuid"])
                elif item["what"]=="vm":
                    vim.delete_vminstance(item["uuid"])
            except vimconn.vimconnException as e:
                logger.error("Error in rollback. Not possible to delete VIM %s '%s'. Message: %s", item['what'], item["uuid"], str(e))
                undeleted_items.append("{} {} from VIM {}".format(item['what'], item["uuid"], vim["name"]))
            except db_base_Exception as e:
                logger.error("Error in rollback. Not possible to delete %s '%s' from DB.datacenters Message: %s", item['what'], item["uuid"], str(e))

        else: # where==mano
            try:
                if item["what"]=="image":
                    mydb.delete_row(FROM="images", WHERE={"uuid": item["uuid"]})
                elif item["what"]=="flavor":
                    mydb.delete_row(FROM="flavors", WHERE={"uuid": item["uuid"]})
            except db_base_Exception as e:
                logger.error("Error in rollback. Not possible to delete %s '%s' from DB. Message: %s", item['what'], item["uuid"], str(e))
                undeleted_items.append("{} '{}'".format(item['what'], item["uuid"]))
    if len(undeleted_items)==0:
        return True," Rollback successful."
    else:
        return False," Rollback fails to delete: " + str(undeleted_items)


def check_vnf_descriptor(vnf_descriptor, vnf_descriptor_version=1):
    global global_config
    #create a dictionary with vnfc-name: vnfc:interface-list  key:values pairs
    vnfc_interfaces={}
    for vnfc in vnf_descriptor["vnf"]["VNFC"]:
        name_dict = {}
        #dataplane interfaces
        for numa in vnfc.get("numas",() ):
            for interface in numa.get("interfaces",()):
                if interface["name"] in name_dict:
                    raise NfvoException(
                        "Error at vnf:VNFC[name:'{}']:numas:interfaces:name, interface name '{}' already used in this VNFC".format(
                            vnfc["name"], interface["name"]),
                        httperrors.Bad_Request)
                name_dict[ interface["name"] ] = "underlay"
        #bridge interfaces
        for interface in vnfc.get("bridge-ifaces",() ):
            if interface["name"] in name_dict:
                raise NfvoException(
                    "Error at vnf:VNFC[name:'{}']:bridge-ifaces:name, interface name '{}' already used in this VNFC".format(
                        vnfc["name"], interface["name"]),
                    httperrors.Bad_Request)
            name_dict[ interface["name"] ] = "overlay"
        vnfc_interfaces[ vnfc["name"] ] = name_dict
        # check bood-data info
        # if "boot-data" in vnfc:
        #     # check that user-data is incompatible with users and config-files
        #     if (vnfc["boot-data"].get("users") or vnfc["boot-data"].get("config-files")) and vnfc["boot-data"].get("user-data"):
        #         raise NfvoException(
        #             "Error at vnf:VNFC:boot-data, fields 'users' and 'config-files' are not compatible with 'user-data'",
        #             httperrors.Bad_Request)

    #check if the info in external_connections matches with the one in the vnfcs
    name_list=[]
    for external_connection in vnf_descriptor["vnf"].get("external-connections",() ):
        if external_connection["name"] in name_list:
            raise NfvoException(
                "Error at vnf:external-connections:name, value '{}' already used as an external-connection".format(
                    external_connection["name"]),
                httperrors.Bad_Request)
        name_list.append(external_connection["name"])
        if external_connection["VNFC"] not in vnfc_interfaces:
            raise NfvoException(
                "Error at vnf:external-connections[name:'{}']:VNFC, value '{}' does not match any VNFC".format(
                    external_connection["name"], external_connection["VNFC"]),
                httperrors.Bad_Request)

        if external_connection["local_iface_name"] not in vnfc_interfaces[ external_connection["VNFC"] ]:
            raise NfvoException(
                "Error at vnf:external-connections[name:'{}']:local_iface_name, value '{}' does not match any interface of this VNFC".format(
                    external_connection["name"],
                    external_connection["local_iface_name"]),
                httperrors.Bad_Request )

    #check if the info in internal_connections matches with the one in the vnfcs
    name_list=[]
    for internal_connection in vnf_descriptor["vnf"].get("internal-connections",() ):
        if internal_connection["name"] in name_list:
            raise NfvoException(
                "Error at vnf:internal-connections:name, value '%s' already used as an internal-connection".format(
                    internal_connection["name"]),
                httperrors.Bad_Request)
        name_list.append(internal_connection["name"])
        #We should check that internal-connections of type "ptp" have only 2 elements

        if len(internal_connection["elements"])>2 and (internal_connection.get("type") == "ptp" or internal_connection.get("type") == "e-line"):
            raise NfvoException(
                "Error at 'vnf:internal-connections[name:'{}']:elements', size must be 2 for a '{}' type. Consider change it to '{}' type".format(
                    internal_connection["name"],
                    'ptp' if vnf_descriptor_version==1 else 'e-line',
                    'data' if vnf_descriptor_version==1 else "e-lan"),
                httperrors.Bad_Request)
        for port in internal_connection["elements"]:
            vnf = port["VNFC"]
            iface = port["local_iface_name"]
            if vnf not in vnfc_interfaces:
                raise NfvoException(
                    "Error at vnf:internal-connections[name:'{}']:elements[]:VNFC, value '{}' does not match any VNFC".format(
                        internal_connection["name"], vnf),
                    httperrors.Bad_Request)
            if iface not in vnfc_interfaces[ vnf ]:
                raise NfvoException(
                    "Error at vnf:internal-connections[name:'{}']:elements[]:local_iface_name, value '{}' does not match any interface of this VNFC".format(
                        internal_connection["name"], iface),
                    httperrors.Bad_Request)
                return -httperrors.Bad_Request,
            if vnf_descriptor_version==1 and "type" not in internal_connection:
                if vnfc_interfaces[vnf][iface] == "overlay":
                    internal_connection["type"] = "bridge"
                else:
                    internal_connection["type"] = "data"
            if vnf_descriptor_version==2 and "implementation" not in internal_connection:
                if vnfc_interfaces[vnf][iface] == "overlay":
                    internal_connection["implementation"] = "overlay"
                else:
                    internal_connection["implementation"] = "underlay"
            if (internal_connection.get("type") == "data" or internal_connection.get("type") == "ptp" or \
                internal_connection.get("implementation") == "underlay") and vnfc_interfaces[vnf][iface] == "overlay":
                raise NfvoException(
                    "Error at vnf:internal-connections[name:'{}']:elements[]:{}, interface of type {} connected to an {} network".format(
                        internal_connection["name"],
                        iface, 'bridge' if vnf_descriptor_version==1 else 'overlay',
                        'data' if vnf_descriptor_version==1 else 'underlay'),
                    httperrors.Bad_Request)
            if (internal_connection.get("type") == "bridge" or internal_connection.get("implementation") == "overlay") and \
                vnfc_interfaces[vnf][iface] == "underlay":
                raise NfvoException(
                    "Error at vnf:internal-connections[name:'{}']:elements[]:{}, interface of type {} connected to an {} network".format(
                        internal_connection["name"], iface,
                        'data' if vnf_descriptor_version==1 else 'underlay',
                        'bridge' if vnf_descriptor_version==1 else 'overlay'),
                    httperrors.Bad_Request)


def create_or_use_image(mydb, vims, image_dict, rollback_list, only_create_at_vim=False, return_on_error=None):
    #look if image exist
    if only_create_at_vim:
        image_mano_id = image_dict['uuid']
        if return_on_error == None:
            return_on_error = True
    else:
        if image_dict['location']:
            images = mydb.get_rows(FROM="images", WHERE={'location':image_dict['location'], 'metadata':image_dict['metadata']})
        else:
            images = mydb.get_rows(FROM="images", WHERE={'universal_name':image_dict['universal_name'], 'checksum':image_dict['checksum']})
        if len(images)>=1:
            image_mano_id = images[0]['uuid']
        else:
            #create image in MANO DB
            temp_image_dict={'name':image_dict['name'],         'description':image_dict.get('description',None),
                            'location':image_dict['location'],  'metadata':image_dict.get('metadata',None),
                            'universal_name':image_dict['universal_name'] , 'checksum':image_dict['checksum']
                            }
            #temp_image_dict['location'] = image_dict.get('new_location') if image_dict['location'] is None
            image_mano_id = mydb.new_row('images', temp_image_dict, add_uuid=True)
            rollback_list.append({"where":"mano", "what":"image","uuid":image_mano_id})
    #create image at every vim
    for vim_id,vim in vims.iteritems():
        datacenter_vim_id = vim["config"]["datacenter_tenant_id"]
        image_created="false"
        #look at database
        image_db = mydb.get_rows(FROM="datacenters_images",
                                 WHERE={'datacenter_vim_id': datacenter_vim_id, 'image_id': image_mano_id})
        #look at VIM if this image exist
        try:
            if image_dict['location'] is not None:
                image_vim_id = vim.get_image_id_from_path(image_dict['location'])
            else:
                filter_dict = {}
                filter_dict['name'] = image_dict['universal_name']
                if image_dict.get('checksum') != None:
                    filter_dict['checksum'] = image_dict['checksum']
                #logger.debug('>>>>>>>> Filter dict: %s', str(filter_dict))
                vim_images = vim.get_image_list(filter_dict)
                #logger.debug('>>>>>>>> VIM images: %s', str(vim_images))
                if len(vim_images) > 1:
                    raise vimconn.vimconnException("More than one candidate VIM image found for filter: {}".format(str(filter_dict)), httperrors.Conflict)
                elif len(vim_images) == 0:
                    raise vimconn.vimconnNotFoundException("Image not found at VIM with filter: '{}'".format(str(filter_dict)))
                else:
                    #logger.debug('>>>>>>>> VIM image 0: %s', str(vim_images[0]))
                    image_vim_id = vim_images[0]['id']

        except vimconn.vimconnNotFoundException as e:
            #Create the image in VIM only if image_dict['location'] or image_dict['new_location'] is not None
            try:
                #image_dict['location']=image_dict.get('new_location') if image_dict['location'] is None
                if image_dict['location']:
                    image_vim_id = vim.new_image(image_dict)
                    rollback_list.append({"where":"vim", "vim_id": vim_id, "what":"image","uuid":image_vim_id})
                    image_created="true"
                else:
                    #If we reach this point, then the image has image name, and optionally checksum, and could not be found
                    raise vimconn.vimconnException(str(e))
            except vimconn.vimconnException as e:
                if return_on_error:
                    logger.error("Error creating image at VIM '%s': %s", vim["name"], str(e))
                    raise
                image_vim_id = None
                logger.warn("Error creating image at VIM '%s': %s", vim["name"], str(e))
                continue
        except vimconn.vimconnException as e:
            if return_on_error:
                logger.error("Error contacting VIM to know if the image exists at VIM: %s", str(e))
                raise
            logger.warn("Error contacting VIM to know if the image exists at VIM: %s", str(e))
            image_vim_id = None
            continue
        #if we reach here, the image has been created or existed
        if len(image_db)==0:
            #add new vim_id at datacenters_images
            mydb.new_row('datacenters_images', {'datacenter_vim_id': datacenter_vim_id,
                                                'image_id':image_mano_id,
                                                'vim_id': image_vim_id,
                                                'created':image_created})
        elif image_db[0]["vim_id"]!=image_vim_id:
            #modify existing vim_id at datacenters_images
            mydb.update_rows('datacenters_images', UPDATE={'vim_id':image_vim_id}, WHERE={'datacenter_vim_id':vim_id, 'image_id':image_mano_id})

    return image_vim_id if only_create_at_vim else image_mano_id


def create_or_use_flavor(mydb, vims, flavor_dict, rollback_list, only_create_at_vim=False, return_on_error = None):
    temp_flavor_dict= {'disk':flavor_dict.get('disk',0),
            'ram':flavor_dict.get('ram'),
            'vcpus':flavor_dict.get('vcpus'),
        }
    if 'extended' in flavor_dict and flavor_dict['extended']==None:
        del flavor_dict['extended']
    if 'extended' in flavor_dict:
        temp_flavor_dict['extended']=yaml.safe_dump(flavor_dict['extended'],default_flow_style=True,width=256)

    #look if flavor exist
    if only_create_at_vim:
        flavor_mano_id = flavor_dict['uuid']
        if return_on_error == None:
            return_on_error = True
    else:
        flavors = mydb.get_rows(FROM="flavors", WHERE=temp_flavor_dict)
        if len(flavors)>=1:
            flavor_mano_id = flavors[0]['uuid']
        else:
            #create flavor
            #create one by one the images of aditional disks
            dev_image_list=[] #list of images
            if 'extended' in flavor_dict and flavor_dict['extended']!=None:
                dev_nb=0
                for device in flavor_dict['extended'].get('devices',[]):
                    if "image" not in device and "image name" not in device:
                        continue
                    image_dict={}
                    image_dict['name']=device.get('image name',flavor_dict['name']+str(dev_nb)+"-img")
                    image_dict['universal_name']=device.get('image name')
                    image_dict['description']=flavor_dict['name']+str(dev_nb)+"-img"
                    image_dict['location']=device.get('image')
                    #image_dict['new_location']=vnfc.get('image location')
                    image_dict['checksum']=device.get('image checksum')
                    image_metadata_dict = device.get('image metadata', None)
                    image_metadata_str = None
                    if image_metadata_dict != None:
                        image_metadata_str = yaml.safe_dump(image_metadata_dict,default_flow_style=True,width=256)
                    image_dict['metadata']=image_metadata_str
                    image_id = create_or_use_image(mydb, vims, image_dict, rollback_list)
                    #print "Additional disk image id for VNFC %s: %s" % (flavor_dict['name']+str(dev_nb)+"-img", image_id)
                    dev_image_list.append(image_id)
                    dev_nb += 1
            temp_flavor_dict['name'] = flavor_dict['name']
            temp_flavor_dict['description'] = flavor_dict.get('description',None)
            content = mydb.new_row('flavors', temp_flavor_dict, add_uuid=True)
            flavor_mano_id= content
            rollback_list.append({"where":"mano", "what":"flavor","uuid":flavor_mano_id})
    #create flavor at every vim
    if 'uuid' in flavor_dict:
        del flavor_dict['uuid']
    flavor_vim_id=None
    for vim_id,vim in vims.items():
        datacenter_vim_id = vim["config"]["datacenter_tenant_id"]
        flavor_created="false"
        #look at database
        flavor_db = mydb.get_rows(FROM="datacenters_flavors",
                                  WHERE={'datacenter_vim_id': datacenter_vim_id, 'flavor_id': flavor_mano_id})
        #look at VIM if this flavor exist  SKIPPED
        #res_vim, flavor_vim_id = vim.get_flavor_id_from_path(flavor_dict['location'])
        #if res_vim < 0:
        #    print "Error contacting VIM to know if the flavor %s existed previously." %flavor_vim_id
        #    continue
        #elif res_vim==0:

        # Create the flavor in VIM
        # Translate images at devices from MANO id to VIM id
        disk_list = []
        if 'extended' in flavor_dict and flavor_dict['extended']!=None and "devices" in flavor_dict['extended']:
            # make a copy of original devices
            devices_original=[]

            for device in flavor_dict["extended"].get("devices",[]):
                dev={}
                dev.update(device)
                devices_original.append(dev)
                if 'image' in device:
                    del device['image']
                if 'image metadata' in device:
                    del device['image metadata']
                if 'image checksum' in device:
                    del device['image checksum']
            dev_nb = 0
            for index in range(0,len(devices_original)) :
                device=devices_original[index]
                if "image" not in device and "image name" not in device:
                    # if 'size' in device:
                    disk_list.append({'size': device.get('size', default_volume_size), 'name': device.get('name')})
                    continue
                image_dict={}
                image_dict['name']=device.get('image name',flavor_dict['name']+str(dev_nb)+"-img")
                image_dict['universal_name']=device.get('image name')
                image_dict['description']=flavor_dict['name']+str(dev_nb)+"-img"
                image_dict['location']=device.get('image')
                # image_dict['new_location']=device.get('image location')
                image_dict['checksum']=device.get('image checksum')
                image_metadata_dict = device.get('image metadata', None)
                image_metadata_str = None
                if image_metadata_dict != None:
                    image_metadata_str = yaml.safe_dump(image_metadata_dict,default_flow_style=True,width=256)
                image_dict['metadata']=image_metadata_str
                image_mano_id=create_or_use_image(mydb, vims, image_dict, rollback_list, only_create_at_vim=False, return_on_error=return_on_error )
                image_dict["uuid"]=image_mano_id
                image_vim_id=create_or_use_image(mydb, vims, image_dict, rollback_list, only_create_at_vim=True, return_on_error=return_on_error)

                #save disk information (image must be based on and size
                disk_list.append({'image_id': image_vim_id, 'size': device.get('size', default_volume_size)})

                flavor_dict["extended"]["devices"][index]['imageRef']=image_vim_id
                dev_nb += 1
        if len(flavor_db)>0:
            #check that this vim_id exist in VIM, if not create
            flavor_vim_id=flavor_db[0]["vim_id"]
            try:
                vim.get_flavor(flavor_vim_id)
                continue #flavor exist
            except vimconn.vimconnException:
                pass
        #create flavor at vim
        logger.debug("nfvo.create_or_use_flavor() adding flavor to VIM %s", vim["name"])
        try:
            flavor_vim_id = None
            flavor_vim_id=vim.get_flavor_id_from_data(flavor_dict)
            flavor_create="false"
        except vimconn.vimconnException as e:
            pass
        try:
            if not flavor_vim_id:
                flavor_vim_id = vim.new_flavor(flavor_dict)
                rollback_list.append({"where":"vim", "vim_id": vim_id, "what":"flavor","uuid":flavor_vim_id})
                flavor_created="true"
        except vimconn.vimconnException as e:
            if return_on_error:
                logger.error("Error creating flavor at VIM %s: %s.", vim["name"], str(e))
                raise
            logger.warn("Error creating flavor at VIM %s: %s.", vim["name"], str(e))
            flavor_vim_id = None
            continue
        #if reach here the flavor has been create or exist
        if len(flavor_db)==0:
            #add new vim_id at datacenters_flavors
            extended_devices_yaml = None
            if len(disk_list) > 0:
                extended_devices = dict()
                extended_devices['disks'] = disk_list
                extended_devices_yaml = yaml.safe_dump(extended_devices,default_flow_style=True,width=256)
            mydb.new_row('datacenters_flavors',
                        {'datacenter_vim_id': datacenter_vim_id, 'flavor_id': flavor_mano_id, 'vim_id': flavor_vim_id,
                        'created': flavor_created, 'extended': extended_devices_yaml})
        elif flavor_db[0]["vim_id"]!=flavor_vim_id:
            #modify existing vim_id at datacenters_flavors
            mydb.update_rows('datacenters_flavors', UPDATE={'vim_id':flavor_vim_id},
                             WHERE={'datacenter_vim_id': datacenter_vim_id, 'flavor_id': flavor_mano_id})

    return flavor_vim_id if only_create_at_vim else flavor_mano_id


def get_str(obj, field, length):
    """
    Obtain the str value,
    :param obj:
    :param length:
    :return:
    """
    value = obj.get(field)
    if value is not None:
        value = str(value)[:length]
    return value

def _lookfor_or_create_image(db_image, mydb, descriptor):
    """
    fill image content at db_image dictionary. Check if the image with this image and checksum exist
    :param db_image: dictionary to insert data
    :param mydb: database connector
    :param descriptor: yang descriptor
    :return: uuid if the image exist at DB, or None if a new image must be created with the data filled at db_image
    """

    db_image["name"] = get_str(descriptor, "image", 255)
    db_image["checksum"] = get_str(descriptor, "image-checksum", 32)
    if not db_image["checksum"]:  # Ensure that if empty string, None is stored
        db_image["checksum"] = None
    if db_image["name"].startswith("/"):
        db_image["location"] = db_image["name"]
        existing_images = mydb.get_rows(FROM="images", WHERE={'location': db_image["location"]})
    else:
        db_image["universal_name"] = db_image["name"]
        existing_images = mydb.get_rows(FROM="images", WHERE={'universal_name': db_image['universal_name'],
                                                              'checksum': db_image['checksum']})
    if existing_images:
        return existing_images[0]["uuid"]
    else:
        image_uuid = str(uuid4())
        db_image["uuid"] = image_uuid
        return None

def get_resource_allocation_params(quota_descriptor):
    """
    read the quota_descriptor from vnfd and fetch the resource allocation properties from the descriptor object
    :param quota_descriptor: cpu/mem/vif/disk-io quota descriptor
    :return: quota params for limit, reserve, shares from the descriptor object
    """
    quota = {}
    if quota_descriptor.get("limit"):
        quota["limit"] = int(quota_descriptor["limit"])
    if quota_descriptor.get("reserve"):
        quota["reserve"] = int(quota_descriptor["reserve"])
    if quota_descriptor.get("shares"):
        quota["shares"] = int(quota_descriptor["shares"])
    return quota

def new_vnfd_v3(mydb, tenant_id, vnf_descriptor):
    """
    Parses an OSM IM vnfd_catalog and insert at DB
    :param mydb:
    :param tenant_id:
    :param vnf_descriptor:
    :return: The list of cretated vnf ids
    """
    try:
        myvnfd = vnfd_catalog.vnfd()
        try:
            pybindJSONDecoder.load_ietf_json(vnf_descriptor, None, None, obj=myvnfd, path_helper=True,
                                             skip_unknown=True)
        except Exception as e:
            raise NfvoException("Error. Invalid VNF descriptor format " + str(e), httperrors.Bad_Request)
        db_vnfs = []
        db_nets = []
        db_vms = []
        db_vms_index = 0
        db_interfaces = []
        db_images = []
        db_flavors = []
        db_ip_profiles_index = 0
        db_ip_profiles = []
        uuid_list = []
        vnfd_uuid_list = []
        vnfd_catalog_descriptor = vnf_descriptor.get("vnfd:vnfd-catalog")
        if not vnfd_catalog_descriptor:
            vnfd_catalog_descriptor = vnf_descriptor.get("vnfd-catalog")
        vnfd_descriptor_list = vnfd_catalog_descriptor.get("vnfd")
        if not vnfd_descriptor_list:
            vnfd_descriptor_list = vnfd_catalog_descriptor.get("vnfd:vnfd")
        for vnfd_yang in myvnfd.vnfd_catalog.vnfd.itervalues():
            vnfd = vnfd_yang.get()

            # table vnf
            vnf_uuid = str(uuid4())
            uuid_list.append(vnf_uuid)
            vnfd_uuid_list.append(vnf_uuid)
            vnfd_id = get_str(vnfd, "id", 255)
            db_vnf = {
                "uuid": vnf_uuid,
                "osm_id": vnfd_id,
                "name": get_str(vnfd, "name", 255),
                "description": get_str(vnfd, "description", 255),
                "tenant_id": tenant_id,
                "vendor": get_str(vnfd, "vendor", 255),
                "short_name": get_str(vnfd, "short-name", 255),
                "descriptor": str(vnf_descriptor)[:60000]
            }

            for vnfd_descriptor in vnfd_descriptor_list:
                if vnfd_descriptor["id"] == str(vnfd["id"]):
                    break

            # table ip_profiles (ip-profiles)
            ip_profile_name2db_table_index = {}
            for ip_profile in vnfd.get("ip-profiles").itervalues():
                db_ip_profile = {
                    "ip_version": str(ip_profile["ip-profile-params"].get("ip-version", "ipv4")),
                    "subnet_address": str(ip_profile["ip-profile-params"].get("subnet-address")),
                    "gateway_address": str(ip_profile["ip-profile-params"].get("gateway-address")),
                    "dhcp_enabled": str(ip_profile["ip-profile-params"]["dhcp-params"].get("enabled", True)),
                    "dhcp_start_address": str(ip_profile["ip-profile-params"]["dhcp-params"].get("start-address")),
                    "dhcp_count": str(ip_profile["ip-profile-params"]["dhcp-params"].get("count")),
                }
                dns_list = []
                for dns in ip_profile["ip-profile-params"]["dns-server"].itervalues():
                    dns_list.append(str(dns.get("address")))
                db_ip_profile["dns_address"] = ";".join(dns_list)
                if ip_profile["ip-profile-params"].get('security-group'):
                    db_ip_profile["security_group"] = ip_profile["ip-profile-params"]['security-group']
                ip_profile_name2db_table_index[str(ip_profile["name"])] = db_ip_profiles_index
                db_ip_profiles_index += 1
                db_ip_profiles.append(db_ip_profile)

            # table nets (internal-vld)
            net_id2uuid = {}  # for mapping interface with network
            for vld in vnfd.get("internal-vld").itervalues():
                net_uuid = str(uuid4())
                uuid_list.append(net_uuid)
                db_net = {
                    "name": get_str(vld, "name", 255),
                    "vnf_id": vnf_uuid,
                    "uuid": net_uuid,
                    "description": get_str(vld, "description", 255),
                    "osm_id": get_str(vld, "id", 255),
                    "type": "bridge",   # TODO adjust depending on connection point type
                }
                net_id2uuid[vld.get("id")] = net_uuid
                db_nets.append(db_net)
                # ip-profile, link db_ip_profile with db_sce_net
                if vld.get("ip-profile-ref"):
                    ip_profile_name = vld.get("ip-profile-ref")
                    if ip_profile_name not in ip_profile_name2db_table_index:
                        raise NfvoException("Error. Invalid VNF descriptor at 'vnfd[{}]':'vld[{}]':'ip-profile-ref':"
                                            "'{}'. Reference to a non-existing 'ip_profiles'".format(
                                                str(vnfd["id"]), str(vld["id"]), str(vld["ip-profile-ref"])),
                                            httperrors.Bad_Request)
                    db_ip_profiles[ip_profile_name2db_table_index[ip_profile_name]]["net_id"] = net_uuid
                else:  #check no ip-address has been defined
                    for icp in vld.get("internal-connection-point").itervalues():
                        if icp.get("ip-address"):
                            raise NfvoException("Error at 'vnfd[{}]':'vld[{}]':'internal-connection-point[{}]' "
                                            "contains an ip-address but no ip-profile has been defined at VLD".format(
                                                str(vnfd["id"]), str(vld["id"]), str(icp["id"])),
                                            httperrors.Bad_Request)

            # connection points vaiable declaration
            cp_name2iface_uuid = {}
            cp_name2vm_uuid = {}
            cp_name2db_interface = {}
            vdu_id2cp_name = {}  # stored only when one external connection point is presented at this VDU

            # table vms (vdus)
            vdu_id2uuid = {}
            vdu_id2db_table_index = {}
            for vdu in vnfd.get("vdu").itervalues():

                for vdu_descriptor in vnfd_descriptor["vdu"]:
                    if vdu_descriptor["id"] == str(vdu["id"]):
                        break
                vm_uuid = str(uuid4())
                uuid_list.append(vm_uuid)
                vdu_id = get_str(vdu, "id", 255)
                db_vm = {
                    "uuid": vm_uuid,
                    "osm_id": vdu_id,
                    "name": get_str(vdu, "name", 255),
                    "description": get_str(vdu, "description", 255),
                    "pdu_type": get_str(vdu, "pdu-type", 255),
                    "vnf_id": vnf_uuid,
                }
                vdu_id2uuid[db_vm["osm_id"]] = vm_uuid
                vdu_id2db_table_index[db_vm["osm_id"]] = db_vms_index
                if vdu.get("count"):
                    db_vm["count"] = int(vdu["count"])

                # table image
                image_present = False
                if vdu.get("image"):
                    image_present = True
                    db_image = {}
                    image_uuid = _lookfor_or_create_image(db_image, mydb, vdu)
                    if not image_uuid:
                        image_uuid = db_image["uuid"]
                        db_images.append(db_image)
                    db_vm["image_id"] = image_uuid
                if vdu.get("alternative-images"):
                    vm_alternative_images = []
                    for alt_image in vdu.get("alternative-images").itervalues():
                        db_image = {}
                        image_uuid = _lookfor_or_create_image(db_image, mydb, alt_image)
                        if not image_uuid:
                            image_uuid = db_image["uuid"]
                            db_images.append(db_image)
                        vm_alternative_images.append({
                            "image_id": image_uuid,
                            "vim_type": str(alt_image["vim-type"]),
                            # "universal_name": str(alt_image["image"]),
                            # "checksum": str(alt_image["image-checksum"]) if alt_image.get("image-checksum") else None
                        })

                    db_vm["image_list"] = yaml.safe_dump(vm_alternative_images, default_flow_style=True, width=256)

                # volumes
                devices = []
                if vdu.get("volumes"):
                    for volume_key in vdu["volumes"]:
                        volume = vdu["volumes"][volume_key]
                        if not image_present:
                            # Convert the first volume to vnfc.image
                            image_present = True
                            db_image = {}
                            image_uuid = _lookfor_or_create_image(db_image, mydb, volume)
                            if not image_uuid:
                                image_uuid = db_image["uuid"]
                                db_images.append(db_image)
                            db_vm["image_id"] = image_uuid
                        else:
                            # Add Openmano devices
                            device = {"name": str(volume.get("name"))}
                            device["type"] = str(volume.get("device-type"))
                            if volume.get("size"):
                                device["size"] = int(volume["size"])
                            if volume.get("image"):
                                device["image name"] = str(volume["image"])
                                if volume.get("image-checksum"):
                                    device["image checksum"] = str(volume["image-checksum"])

                            devices.append(device)

                if not db_vm.get("image_id"):
                    if not db_vm["pdu_type"]:
                        raise NfvoException("Not defined image for VDU")
                    # create a fake image

                # cloud-init
                boot_data = {}
                if vdu.get("cloud-init"):
                    boot_data["user-data"] = str(vdu["cloud-init"])
                elif vdu.get("cloud-init-file"):
                    # TODO Where this file content is present???
                    # boot_data["user-data"] = vnfd_yang.files[vdu["cloud-init-file"]]
                    boot_data["user-data"] = str(vdu["cloud-init-file"])

                if vdu.get("supplemental-boot-data"):
                    if vdu["supplemental-boot-data"].get('boot-data-drive'):
                            boot_data['boot-data-drive'] = True
                    if vdu["supplemental-boot-data"].get('config-file'):
                        om_cfgfile_list = list()
                        for custom_config_file in vdu["supplemental-boot-data"]['config-file'].itervalues():
                            # TODO Where this file content is present???
                            cfg_source = str(custom_config_file["source"])
                            om_cfgfile_list.append({"dest": custom_config_file["dest"],
                                                    "content": cfg_source})
                        boot_data['config-files'] = om_cfgfile_list
                if boot_data:
                    db_vm["boot_data"] = yaml.safe_dump(boot_data, default_flow_style=True, width=256)

                db_vms.append(db_vm)
                db_vms_index += 1

                # table interfaces (internal/external interfaces)
                flavor_epa_interfaces = []
                # for iface in chain(vdu.get("internal-interface").itervalues(), vdu.get("external-interface").itervalues()):
                for iface in vdu.get("interface").itervalues():
                    flavor_epa_interface = {}
                    iface_uuid = str(uuid4())
                    uuid_list.append(iface_uuid)
                    db_interface = {
                        "uuid": iface_uuid,
                        "internal_name": get_str(iface, "name", 255),
                        "vm_id": vm_uuid,
                    }
                    flavor_epa_interface["name"] = db_interface["internal_name"]
                    if iface.get("virtual-interface").get("vpci"):
                        db_interface["vpci"] = get_str(iface.get("virtual-interface"), "vpci", 12)
                        flavor_epa_interface["vpci"] = db_interface["vpci"]

                    if iface.get("virtual-interface").get("bandwidth"):
                        bps = int(iface.get("virtual-interface").get("bandwidth"))
                        db_interface["bw"] = int(math.ceil(bps/1000000.0))
                        flavor_epa_interface["bandwidth"] = "{} Mbps".format(db_interface["bw"])

                    if iface.get("virtual-interface").get("type") == "OM-MGMT":
                        db_interface["type"] = "mgmt"
                    elif iface.get("virtual-interface").get("type") in ("VIRTIO", "E1000", "PARAVIRT"):
                        db_interface["type"] = "bridge"
                        db_interface["model"] = get_str(iface.get("virtual-interface"), "type", 12)
                    elif iface.get("virtual-interface").get("type") in ("SR-IOV", "PCI-PASSTHROUGH"):
                        db_interface["type"] = "data"
                        db_interface["model"] = get_str(iface.get("virtual-interface"), "type", 12)
                        flavor_epa_interface["dedicated"] = "no" if iface["virtual-interface"]["type"] == "SR-IOV" \
                            else "yes"
                        flavor_epa_interfaces.append(flavor_epa_interface)
                    else:
                        raise NfvoException("Error. Invalid VNF descriptor at 'vnfd[{}]':'vdu[{}]':'interface':'virtual"
                                            "-interface':'type':'{}'. Interface type is not supported".format(
                                                vnfd_id, vdu_id, iface.get("virtual-interface").get("type")),
                                            httperrors.Bad_Request)

                    if iface.get("mgmt-interface"):
                        db_interface["type"] = "mgmt"

                    if iface.get("external-connection-point-ref"):
                        try:
                            cp = vnfd.get("connection-point")[iface.get("external-connection-point-ref")]
                            db_interface["external_name"] = get_str(cp, "name", 255)
                            cp_name2iface_uuid[db_interface["external_name"]] = iface_uuid
                            cp_name2vm_uuid[db_interface["external_name"]] = vm_uuid
                            cp_name2db_interface[db_interface["external_name"]] = db_interface
                            for cp_descriptor in vnfd_descriptor["connection-point"]:
                                if cp_descriptor["name"] == db_interface["external_name"]:
                                    break
                            else:
                                raise KeyError()

                            if vdu_id in vdu_id2cp_name:
                                vdu_id2cp_name[vdu_id] = None  # more than two connecdtion point for this VDU
                            else:
                                vdu_id2cp_name[vdu_id] = db_interface["external_name"]

                            # port security
                            if str(cp_descriptor.get("port-security-enabled")).lower() == "false":
                                db_interface["port_security"] = 0
                            elif str(cp_descriptor.get("port-security-enabled")).lower() == "true":
                                db_interface["port_security"] = 1
                        except KeyError:
                            raise NfvoException("Error. Invalid VNF descriptor at 'vnfd[{vnf}]':'vdu[{vdu}]':"
                                                "'interface[{iface}]':'vnfd-connection-point-ref':'{cp}' is not present"
                                                " at connection-point".format(
                                                    vnf=vnfd_id, vdu=vdu_id, iface=iface["name"],
                                                    cp=iface.get("vnfd-connection-point-ref")),
                                                httperrors.Bad_Request)
                    elif iface.get("internal-connection-point-ref"):
                        try:
                            for icp_descriptor in vdu_descriptor["internal-connection-point"]:
                                if icp_descriptor["id"] == str(iface.get("internal-connection-point-ref")):
                                    break
                            else:
                                raise KeyError("does not exist at vdu:internal-connection-point")
                            icp = None
                            icp_vld = None
                            for vld in vnfd.get("internal-vld").itervalues():
                                for cp in vld.get("internal-connection-point").itervalues():
                                    if cp.get("id-ref") == iface.get("internal-connection-point-ref"):
                                        if icp:
                                            raise KeyError("is referenced by more than one 'internal-vld'")
                                        icp = cp
                                        icp_vld = vld
                            if not icp:
                                raise KeyError("is not referenced by any 'internal-vld'")

                            db_interface["net_id"] = net_id2uuid[icp_vld.get("id")]
                            if str(icp_descriptor.get("port-security-enabled")).lower() == "false":
                                db_interface["port_security"] = 0
                            elif str(icp_descriptor.get("port-security-enabled")).lower() == "true":
                                db_interface["port_security"] = 1
                            if icp.get("ip-address"):
                                if not icp_vld.get("ip-profile-ref"):
                                    raise NfvoException
                                db_interface["ip_address"] = str(icp.get("ip-address"))
                        except KeyError as e:
                            raise NfvoException("Error. Invalid VNF descriptor at 'vnfd[{vnf}]':'vdu[{vdu}]':"
                                                "'interface[{iface}]':'internal-connection-point-ref':'{cp}'"
                                                " {msg}".format(
                                                    vnf=vnfd_id, vdu=vdu_id, iface=iface["name"],
                                                    cp=iface.get("internal-connection-point-ref"), msg=str(e)),
                                                httperrors.Bad_Request)
                    if iface.get("position"):
                        db_interface["created_at"] = int(iface.get("position")) * 50
                    if iface.get("mac-address"):
                        db_interface["mac"] = str(iface.get("mac-address"))
                    db_interfaces.append(db_interface)

                # table flavors
                db_flavor = {
                    "name": get_str(vdu, "name", 250) + "-flv",
                    "vcpus": int(vdu["vm-flavor"].get("vcpu-count", 1)),
                    "ram": int(vdu["vm-flavor"].get("memory-mb", 1)),
                    "disk": int(vdu["vm-flavor"].get("storage-gb", 0)),
                }
                # TODO revise the case of several numa-node-policy node
                extended = {}
                numa = {}
                if devices:
                    extended["devices"] = devices
                if flavor_epa_interfaces:
                    numa["interfaces"] = flavor_epa_interfaces
                if vdu.get("guest-epa"):   # TODO or dedicated_int:
                    epa_vcpu_set = False
                    if vdu["guest-epa"].get("numa-node-policy"):  # TODO or dedicated_int:
                        numa_node_policy = vdu["guest-epa"].get("numa-node-policy")
                        if numa_node_policy.get("node"):
                            numa_node = numa_node_policy["node"].values()[0]
                            if numa_node.get("num-cores"):
                                numa["cores"] = numa_node["num-cores"]
                                epa_vcpu_set = True
                            if numa_node.get("paired-threads"):
                                if numa_node["paired-threads"].get("num-paired-threads"):
                                    numa["paired-threads"] = int(numa_node["paired-threads"]["num-paired-threads"])
                                    epa_vcpu_set = True
                                if len(numa_node["paired-threads"].get("paired-thread-ids")):
                                    numa["paired-threads-id"] = []
                                    for pair in numa_node["paired-threads"]["paired-thread-ids"].itervalues():
                                        numa["paired-threads-id"].append(
                                            (str(pair["thread-a"]), str(pair["thread-b"]))
                                        )
                            if numa_node.get("num-threads"):
                                numa["threads"] = int(numa_node["num-threads"])
                                epa_vcpu_set = True
                            if numa_node.get("memory-mb"):
                                numa["memory"] = max(int(numa_node["memory-mb"] / 1024), 1)
                    if vdu["guest-epa"].get("mempage-size"):
                        if vdu["guest-epa"]["mempage-size"] != "SMALL":
                            numa["memory"] = max(int(db_flavor["ram"] / 1024), 1)
                    if vdu["guest-epa"].get("cpu-pinning-policy") and not epa_vcpu_set:
                        if vdu["guest-epa"]["cpu-pinning-policy"] == "DEDICATED":
                            if vdu["guest-epa"].get("cpu-thread-pinning-policy") and \
                                            vdu["guest-epa"]["cpu-thread-pinning-policy"] != "PREFER":
                                numa["cores"] = max(db_flavor["vcpus"], 1)
                            else:
                                numa["threads"] = max(db_flavor["vcpus"], 1)
                            epa_vcpu_set = True
                    if vdu["guest-epa"].get("cpu-quota") and not epa_vcpu_set:
                        extended["cpu-quota"] = get_resource_allocation_params(vdu["guest-epa"].get("cpu-quota"))
                    if vdu["guest-epa"].get("mem-quota"):
                        extended["mem-quota"] = get_resource_allocation_params(vdu["guest-epa"].get("mem-quota"))
                    if vdu["guest-epa"].get("disk-io-quota"):
                        extended["disk-io-quota"] = get_resource_allocation_params(vdu["guest-epa"].get("disk-io-quota"))
                    if vdu["guest-epa"].get("vif-quota"):
                        extended["vif-quota"] = get_resource_allocation_params(vdu["guest-epa"].get("vif-quota"))
                if numa:
                    extended["numas"] = [numa]
                if extended:
                    extended_text = yaml.safe_dump(extended, default_flow_style=True, width=256)
                    db_flavor["extended"] = extended_text
                # look if flavor exist
                temp_flavor_dict = {'disk': db_flavor.get('disk', 0),
                                    'ram': db_flavor.get('ram'),
                                    'vcpus': db_flavor.get('vcpus'),
                                    'extended': db_flavor.get('extended')
                                    }
                existing_flavors = mydb.get_rows(FROM="flavors", WHERE=temp_flavor_dict)
                if existing_flavors:
                    flavor_uuid = existing_flavors[0]["uuid"]
                else:
                    flavor_uuid = str(uuid4())
                    uuid_list.append(flavor_uuid)
                    db_flavor["uuid"] = flavor_uuid
                    db_flavors.append(db_flavor)
                db_vm["flavor_id"] = flavor_uuid

            # VNF affinity and antiaffinity
            for pg in vnfd.get("placement-groups").itervalues():
                pg_name = get_str(pg, "name", 255)
                for vdu in pg.get("member-vdus").itervalues():
                    vdu_id = get_str(vdu, "member-vdu-ref", 255)
                    if vdu_id not in vdu_id2db_table_index:
                        raise NfvoException("Error. Invalid VNF descriptor at 'vnfd[{vnf}]':'placement-groups[{pg}]':"
                                            "'member-vdus':'{vdu}'. Reference to a non-existing vdu".format(
                                                vnf=vnfd_id, pg=pg_name, vdu=vdu_id),
                                            httperrors.Bad_Request)
                    db_vms[vdu_id2db_table_index[vdu_id]]["availability_zone"] = pg_name
                    # TODO consider the case of isolation and not colocation
                    # if pg.get("strategy") == "ISOLATION":

            # VNF mgmt configuration
            mgmt_access = {}
            if vnfd["mgmt-interface"].get("vdu-id"):
                mgmt_vdu_id = get_str(vnfd["mgmt-interface"], "vdu-id", 255)
                if mgmt_vdu_id not in vdu_id2uuid:
                    raise NfvoException("Error. Invalid VNF descriptor at 'vnfd[{vnf}]':'mgmt-interface':'vdu-id':"
                                        "'{vdu}'. Reference to a non-existing vdu".format(
                                            vnf=vnfd_id, vdu=mgmt_vdu_id),
                                        httperrors.Bad_Request)
                mgmt_access["vm_id"] = vdu_id2uuid[vnfd["mgmt-interface"]["vdu-id"]]
                # if only one cp is defined by this VDU, mark this interface as of type "mgmt"
                if vdu_id2cp_name.get(mgmt_vdu_id):
                    if cp_name2db_interface[vdu_id2cp_name[mgmt_vdu_id]]:
                        cp_name2db_interface[vdu_id2cp_name[mgmt_vdu_id]]["type"] = "mgmt"

            if vnfd["mgmt-interface"].get("ip-address"):
                mgmt_access["ip-address"] = str(vnfd["mgmt-interface"].get("ip-address"))
            if vnfd["mgmt-interface"].get("cp"):
                if vnfd["mgmt-interface"]["cp"] not in cp_name2iface_uuid:
                    raise NfvoException("Error. Invalid VNF descriptor at 'vnfd[{vnf}]':'mgmt-interface':'cp'['{cp}']. "
                                        "Reference to a non-existing connection-point".format(
                                            vnf=vnfd_id, cp=vnfd["mgmt-interface"]["cp"]),
                                        httperrors.Bad_Request)
                mgmt_access["vm_id"] = cp_name2vm_uuid[vnfd["mgmt-interface"]["cp"]]
                mgmt_access["interface_id"] = cp_name2iface_uuid[vnfd["mgmt-interface"]["cp"]]
                # mark this interface as of type mgmt
                if cp_name2db_interface[vnfd["mgmt-interface"]["cp"]]:
                    cp_name2db_interface[vnfd["mgmt-interface"]["cp"]]["type"] = "mgmt"

            default_user = get_str(vnfd.get("vnf-configuration", {}).get("config-access", {}).get("ssh-access", {}),
                                    "default-user", 64)

            if default_user:
                mgmt_access["default_user"] = default_user
            required = get_str(vnfd.get("vnf-configuration", {}).get("config-access", {}).get("ssh-access", {}),
                                   "required", 6)
            if required:
                mgmt_access["required"] = required

            if mgmt_access:
                db_vnf["mgmt_access"] = yaml.safe_dump(mgmt_access, default_flow_style=True, width=256)

            db_vnfs.append(db_vnf)
        db_tables=[
            {"vnfs": db_vnfs},
            {"nets": db_nets},
            {"images": db_images},
            {"flavors": db_flavors},
            {"ip_profiles": db_ip_profiles},
            {"vms": db_vms},
            {"interfaces": db_interfaces},
        ]

        logger.debug("create_vnf Deployment done vnfDict: %s",
                    yaml.safe_dump(db_tables, indent=4, default_flow_style=False) )
        mydb.new_rows(db_tables, uuid_list)
        return vnfd_uuid_list
    except NfvoException:
        raise
    except Exception as e:
        logger.error("Exception {}".format(e))
        raise  # NfvoException("Exception {}".format(e), httperrors.Bad_Request)


@deprecated("Use new_vnfd_v3")
def new_vnf(mydb, tenant_id, vnf_descriptor):
    global global_config

    # Step 1. Check the VNF descriptor
    check_vnf_descriptor(vnf_descriptor, vnf_descriptor_version=1)
    # Step 2. Check tenant exist
    vims = {}
    if tenant_id != "any":
        check_tenant(mydb, tenant_id)
        if "tenant_id" in vnf_descriptor["vnf"]:
            if vnf_descriptor["vnf"]["tenant_id"] != tenant_id:
                raise NfvoException("VNF can not have a different tenant owner '{}', must be '{}'".format(vnf_descriptor["vnf"]["tenant_id"], tenant_id),
                                    httperrors.Unauthorized)
        else:
            vnf_descriptor['vnf']['tenant_id'] = tenant_id
        # Step 3. Get the URL of the VIM from the nfvo_tenant and the datacenter
        if global_config["auto_push_VNF_to_VIMs"]:
            vims = get_vim(mydb, tenant_id, ignore_errors=True)

    # Step 4. Review the descriptor and add missing  fields
    #print vnf_descriptor
    #logger.debug("Refactoring VNF descriptor with fields: description, public (default: true)")
    vnf_name = vnf_descriptor['vnf']['name']
    vnf_descriptor['vnf']['description'] = vnf_descriptor['vnf'].get("description", vnf_name)
    if "physical" in vnf_descriptor['vnf']:
        del vnf_descriptor['vnf']['physical']
    #print vnf_descriptor

    # Step 6. For each VNFC in the descriptor, flavors and images are created in the VIM
    logger.debug('BEGIN creation of VNF "%s"' % vnf_name)
    logger.debug("VNF %s: consisting of %d VNFC(s)" % (vnf_name,len(vnf_descriptor['vnf']['VNFC'])))

    #For each VNFC, we add it to the VNFCDict and we  create a flavor.
    VNFCDict = {}     # Dictionary, key: VNFC name, value: dict with the relevant information to create the VNF and VMs in the MANO database
    rollback_list = []    # It will contain the new images created in mano. It is used for rollback
    try:
        logger.debug("Creating additional disk images and new flavors in the VIM for each VNFC")
        for vnfc in vnf_descriptor['vnf']['VNFC']:
            VNFCitem={}
            VNFCitem["name"] = vnfc['name']
            VNFCitem["availability_zone"] = vnfc.get('availability_zone')
            VNFCitem["description"] = vnfc.get("description", 'VM %s of the VNF %s' %(vnfc['name'],vnf_name))

            #print "Flavor name: %s. Description: %s" % (VNFCitem["name"]+"-flv", VNFCitem["description"])

            myflavorDict = {}
            myflavorDict["name"] = vnfc['name']+"-flv"   #Maybe we could rename the flavor by using the field "image name" if exists
            myflavorDict["description"] = VNFCitem["description"]
            myflavorDict["ram"] = vnfc.get("ram", 0)
            myflavorDict["vcpus"] = vnfc.get("vcpus", 0)
            myflavorDict["disk"] = vnfc.get("disk", 0)
            myflavorDict["extended"] = {}

            devices = vnfc.get("devices")
            if devices != None:
                myflavorDict["extended"]["devices"] = devices

            # TODO:
            # Mapping from processor models to rankings should be available somehow in the NFVO. They could be taken from VIM or directly from a new database table
            # Another option is that the processor in the VNF descriptor specifies directly the ranking of the host

            # Previous code has been commented
            #if vnfc['processor']['model'] == "Intel(R) Xeon(R) CPU E5-4620 0 @ 2.20GHz" :
            #    myflavorDict["flavor"]['extended']['processor_ranking'] = 200
            #elif vnfc['processor']['model'] == "Intel(R) Xeon(R) CPU E5-2697 v2 @ 2.70GHz" :
            #    myflavorDict["flavor"]['extended']['processor_ranking'] = 300
            #else:
            #    result2, message = rollback(myvim, myvimURL, myvim_tenant, flavorList, imageList)
            #    if result2:
            #        print "Error creating flavor: unknown processor model. Rollback successful."
            #        return -httperrors.Bad_Request, "Error creating flavor: unknown processor model. Rollback successful."
            #    else:
            #        return -httperrors.Bad_Request, "Error creating flavor: unknown processor model. Rollback fail: you need to access VIM and delete the following %s" % message
            myflavorDict['extended']['processor_ranking'] = 100  #Hardcoded value, while we decide when the mapping is done

            if 'numas' in vnfc and len(vnfc['numas'])>0:
                myflavorDict['extended']['numas'] = vnfc['numas']

            #print myflavorDict

            # Step 6.2 New flavors are created in the VIM
            flavor_id = create_or_use_flavor(mydb, vims, myflavorDict, rollback_list)

            #print "Flavor id for VNFC %s: %s" % (vnfc['name'],flavor_id)
            VNFCitem["flavor_id"] = flavor_id
            VNFCDict[vnfc['name']] = VNFCitem

        logger.debug("Creating new images in the VIM for each VNFC")
        # Step 6.3 New images are created in the VIM
        #For each VNFC, we must create the appropriate image.
        #This "for" loop might be integrated with the previous one
        #In case this integration is made, the VNFCDict might become a VNFClist.
        for vnfc in vnf_descriptor['vnf']['VNFC']:
            #print "Image name: %s. Description: %s" % (vnfc['name']+"-img", VNFCDict[vnfc['name']]['description'])
            image_dict={}
            image_dict['name']=vnfc.get('image name',vnf_name+"-"+vnfc['name']+"-img")
            image_dict['universal_name']=vnfc.get('image name')
            image_dict['description']=vnfc.get('image name', VNFCDict[vnfc['name']]['description'])
            image_dict['location']=vnfc.get('VNFC image')
            #image_dict['new_location']=vnfc.get('image location')
            image_dict['checksum']=vnfc.get('image checksum')
            image_metadata_dict = vnfc.get('image metadata', None)
            image_metadata_str = None
            if image_metadata_dict is not None:
                image_metadata_str = yaml.safe_dump(image_metadata_dict,default_flow_style=True,width=256)
            image_dict['metadata']=image_metadata_str
            #print "create_or_use_image", mydb, vims, image_dict, rollback_list
            image_id = create_or_use_image(mydb, vims, image_dict, rollback_list)
            #print "Image id for VNFC %s: %s" % (vnfc['name'],image_id)
            VNFCDict[vnfc['name']]["image_id"] = image_id
            VNFCDict[vnfc['name']]["image_path"] = vnfc.get('VNFC image')
            VNFCDict[vnfc['name']]["count"] = vnfc.get('count', 1)
            if vnfc.get("boot-data"):
                VNFCDict[vnfc['name']]["boot_data"] = yaml.safe_dump(vnfc["boot-data"], default_flow_style=True, width=256)


        # Step 7. Storing the VNF descriptor in the repository
        if "descriptor" not in vnf_descriptor["vnf"]:
            vnf_descriptor["vnf"]["descriptor"] = yaml.safe_dump(vnf_descriptor, indent=4, explicit_start=True, default_flow_style=False)

        # Step 8. Adding the VNF to the NFVO DB
        vnf_id = mydb.new_vnf_as_a_whole(tenant_id,vnf_name,vnf_descriptor,VNFCDict)
        return vnf_id
    except (db_base_Exception, vimconn.vimconnException, KeyError) as e:
        _, message = rollback(mydb, vims, rollback_list)
        if isinstance(e, db_base_Exception):
            error_text = "Exception at database"
        elif isinstance(e, KeyError):
            error_text = "KeyError exception "
            e.http_code = httperrors.Internal_Server_Error
        else:
            error_text = "Exception at VIM"
        error_text += " {} {}. {}".format(type(e).__name__, str(e), message)
        #logger.error("start_scenario %s", error_text)
        raise NfvoException(error_text, e.http_code)


@deprecated("Use new_vnfd_v3")
def new_vnf_v02(mydb, tenant_id, vnf_descriptor):
    global global_config

    # Step 1. Check the VNF descriptor
    check_vnf_descriptor(vnf_descriptor, vnf_descriptor_version=2)
    # Step 2. Check tenant exist
    vims = {}
    if tenant_id != "any":
        check_tenant(mydb, tenant_id)
        if "tenant_id" in vnf_descriptor["vnf"]:
            if vnf_descriptor["vnf"]["tenant_id"] != tenant_id:
                raise NfvoException("VNF can not have a different tenant owner '{}', must be '{}'".format(vnf_descriptor["vnf"]["tenant_id"], tenant_id),
                                    httperrors.Unauthorized)
        else:
            vnf_descriptor['vnf']['tenant_id'] = tenant_id
        # Step 3. Get the URL of the VIM from the nfvo_tenant and the datacenter
        if global_config["auto_push_VNF_to_VIMs"]:
            vims = get_vim(mydb, tenant_id, ignore_errors=True)

    # Step 4. Review the descriptor and add missing  fields
    #print vnf_descriptor
    #logger.debug("Refactoring VNF descriptor with fields: description, public (default: true)")
    vnf_name = vnf_descriptor['vnf']['name']
    vnf_descriptor['vnf']['description'] = vnf_descriptor['vnf'].get("description", vnf_name)
    if "physical" in vnf_descriptor['vnf']:
        del vnf_descriptor['vnf']['physical']
    #print vnf_descriptor

    # Step 6. For each VNFC in the descriptor, flavors and images are created in the VIM
    logger.debug('BEGIN creation of VNF "%s"' % vnf_name)
    logger.debug("VNF %s: consisting of %d VNFC(s)" % (vnf_name,len(vnf_descriptor['vnf']['VNFC'])))

    #For each VNFC, we add it to the VNFCDict and we  create a flavor.
    VNFCDict = {}     # Dictionary, key: VNFC name, value: dict with the relevant information to create the VNF and VMs in the MANO database
    rollback_list = []    # It will contain the new images created in mano. It is used for rollback
    try:
        logger.debug("Creating additional disk images and new flavors in the VIM for each VNFC")
        for vnfc in vnf_descriptor['vnf']['VNFC']:
            VNFCitem={}
            VNFCitem["name"] = vnfc['name']
            VNFCitem["description"] = vnfc.get("description", 'VM %s of the VNF %s' %(vnfc['name'],vnf_name))

            #print "Flavor name: %s. Description: %s" % (VNFCitem["name"]+"-flv", VNFCitem["description"])

            myflavorDict = {}
            myflavorDict["name"] = vnfc['name']+"-flv"   #Maybe we could rename the flavor by using the field "image name" if exists
            myflavorDict["description"] = VNFCitem["description"]
            myflavorDict["ram"] = vnfc.get("ram", 0)
            myflavorDict["vcpus"] = vnfc.get("vcpus", 0)
            myflavorDict["disk"] = vnfc.get("disk", 0)
            myflavorDict["extended"] = {}

            devices = vnfc.get("devices")
            if devices != None:
                myflavorDict["extended"]["devices"] = devices

            # TODO:
            # Mapping from processor models to rankings should be available somehow in the NFVO. They could be taken from VIM or directly from a new database table
            # Another option is that the processor in the VNF descriptor specifies directly the ranking of the host

            # Previous code has been commented
            #if vnfc['processor']['model'] == "Intel(R) Xeon(R) CPU E5-4620 0 @ 2.20GHz" :
            #    myflavorDict["flavor"]['extended']['processor_ranking'] = 200
            #elif vnfc['processor']['model'] == "Intel(R) Xeon(R) CPU E5-2697 v2 @ 2.70GHz" :
            #    myflavorDict["flavor"]['extended']['processor_ranking'] = 300
            #else:
            #    result2, message = rollback(myvim, myvimURL, myvim_tenant, flavorList, imageList)
            #    if result2:
            #        print "Error creating flavor: unknown processor model. Rollback successful."
            #        return -httperrors.Bad_Request, "Error creating flavor: unknown processor model. Rollback successful."
            #    else:
            #        return -httperrors.Bad_Request, "Error creating flavor: unknown processor model. Rollback fail: you need to access VIM and delete the following %s" % message
            myflavorDict['extended']['processor_ranking'] = 100  #Hardcoded value, while we decide when the mapping is done

            if 'numas' in vnfc and len(vnfc['numas'])>0:
                myflavorDict['extended']['numas'] = vnfc['numas']

            #print myflavorDict

            # Step 6.2 New flavors are created in the VIM
            flavor_id = create_or_use_flavor(mydb, vims, myflavorDict, rollback_list)

            #print "Flavor id for VNFC %s: %s" % (vnfc['name'],flavor_id)
            VNFCitem["flavor_id"] = flavor_id
            VNFCDict[vnfc['name']] = VNFCitem

        logger.debug("Creating new images in the VIM for each VNFC")
        # Step 6.3 New images are created in the VIM
        #For each VNFC, we must create the appropriate image.
        #This "for" loop might be integrated with the previous one
        #In case this integration is made, the VNFCDict might become a VNFClist.
        for vnfc in vnf_descriptor['vnf']['VNFC']:
            #print "Image name: %s. Description: %s" % (vnfc['name']+"-img", VNFCDict[vnfc['name']]['description'])
            image_dict={}
            image_dict['name']=vnfc.get('image name',vnf_name+"-"+vnfc['name']+"-img")
            image_dict['universal_name']=vnfc.get('image name')
            image_dict['description']=vnfc.get('image name', VNFCDict[vnfc['name']]['description'])
            image_dict['location']=vnfc.get('VNFC image')
            #image_dict['new_location']=vnfc.get('image location')
            image_dict['checksum']=vnfc.get('image checksum')
            image_metadata_dict = vnfc.get('image metadata', None)
            image_metadata_str = None
            if image_metadata_dict is not None:
                image_metadata_str = yaml.safe_dump(image_metadata_dict,default_flow_style=True,width=256)
            image_dict['metadata']=image_metadata_str
            #print "create_or_use_image", mydb, vims, image_dict, rollback_list
            image_id = create_or_use_image(mydb, vims, image_dict, rollback_list)
            #print "Image id for VNFC %s: %s" % (vnfc['name'],image_id)
            VNFCDict[vnfc['name']]["image_id"] = image_id
            VNFCDict[vnfc['name']]["image_path"] = vnfc.get('VNFC image')
            VNFCDict[vnfc['name']]["count"] = vnfc.get('count', 1)
            if vnfc.get("boot-data"):
                VNFCDict[vnfc['name']]["boot_data"] = yaml.safe_dump(vnfc["boot-data"], default_flow_style=True, width=256)

        # Step 7. Storing the VNF descriptor in the repository
        if "descriptor" not in vnf_descriptor["vnf"]:
            vnf_descriptor["vnf"]["descriptor"] = yaml.safe_dump(vnf_descriptor, indent=4, explicit_start=True, default_flow_style=False)

        # Step 8. Adding the VNF to the NFVO DB
        vnf_id = mydb.new_vnf_as_a_whole2(tenant_id,vnf_name,vnf_descriptor,VNFCDict)
        return vnf_id
    except (db_base_Exception, vimconn.vimconnException, KeyError) as e:
        _, message = rollback(mydb, vims, rollback_list)
        if isinstance(e, db_base_Exception):
            error_text = "Exception at database"
        elif isinstance(e, KeyError):
            error_text = "KeyError exception "
            e.http_code = httperrors.Internal_Server_Error
        else:
            error_text = "Exception at VIM"
        error_text += " {} {}. {}".format(type(e).__name__, str(e), message)
        #logger.error("start_scenario %s", error_text)
        raise NfvoException(error_text, e.http_code)


def get_vnf_id(mydb, tenant_id, vnf_id):
    #check valid tenant_id
    check_tenant(mydb, tenant_id)
    #obtain data
    where_or = {}
    if tenant_id != "any":
        where_or["tenant_id"] = tenant_id
        where_or["public"] = True
    vnf = mydb.get_table_by_uuid_name('vnfs', vnf_id, "VNF", WHERE_OR=where_or, WHERE_AND_OR="AND")

    vnf_id = vnf["uuid"]
    filter_keys = ('uuid', 'name', 'description', 'public', "tenant_id", "osm_id", "created_at")
    filtered_content = dict( (k,v) for k,v in vnf.iteritems() if k in filter_keys )
    #change_keys_http2db(filtered_content, http2db_vnf, reverse=True)
    data={'vnf' : filtered_content}
    #GET VM
    content = mydb.get_rows(FROM='vnfs join vms on vnfs.uuid=vms.vnf_id',
            SELECT=('vms.uuid as uuid', 'vms.osm_id as osm_id', 'vms.name as name', 'vms.description as description',
                    'boot_data'),
            WHERE={'vnfs.uuid': vnf_id} )
    if len(content) != 0:
        #raise NfvoException("vnf '{}' not found".format(vnf_id), httperrors.Not_Found)
    # change boot_data into boot-data
        for vm in content:
            if vm.get("boot_data"):
                vm["boot-data"] = yaml.safe_load(vm["boot_data"])
                del vm["boot_data"]

        data['vnf']['VNFC'] = content
    #TODO: GET all the information from a VNFC and include it in the output.

    #GET NET
    content = mydb.get_rows(FROM='vnfs join nets on vnfs.uuid=nets.vnf_id',
                                    SELECT=('nets.uuid as uuid','nets.name as name','nets.description as description', 'nets.type as type', 'nets.multipoint as multipoint'),
                                    WHERE={'vnfs.uuid': vnf_id} )
    data['vnf']['nets'] = content

    #GET ip-profile for each net
    for net in data['vnf']['nets']:
        ipprofiles = mydb.get_rows(FROM='ip_profiles',
                                   SELECT=('ip_version','subnet_address','gateway_address','dns_address','dhcp_enabled','dhcp_start_address','dhcp_count'),
                                   WHERE={'net_id': net["uuid"]} )
        if len(ipprofiles)==1:
            net["ip_profile"] = ipprofiles[0]
        elif len(ipprofiles)>1:
            raise NfvoException("More than one ip-profile found with this criteria: net_id='{}'".format(net['uuid']), httperrors.Bad_Request)


    #TODO: For each net, GET its elements and relevant info per element (VNFC, iface, ip_address) and include them in the output.

    #GET External Interfaces
    content = mydb.get_rows(FROM='vnfs join vms on vnfs.uuid=vms.vnf_id join interfaces on vms.uuid=interfaces.vm_id',\
                                    SELECT=('interfaces.uuid as uuid','interfaces.external_name as external_name', 'vms.name as vm_name', 'interfaces.vm_id as vm_id', \
                                            'interfaces.internal_name as internal_name', 'interfaces.type as type', 'interfaces.vpci as vpci','interfaces.bw as bw'),\
                                    WHERE={'vnfs.uuid': vnf_id, 'interfaces.external_name<>': None} )
    #print content
    data['vnf']['external-connections'] = content

    return data


def delete_vnf(mydb,tenant_id,vnf_id,datacenter=None,vim_tenant=None):
    # Check tenant exist
    if tenant_id != "any":
        check_tenant(mydb, tenant_id)
        # Get the URL of the VIM from the nfvo_tenant and the datacenter
        vims = get_vim(mydb, tenant_id, ignore_errors=True)
    else:
        vims={}

    # Checking if it is a valid uuid and, if not, getting the uuid assuming that the name was provided"
    where_or = {}
    if tenant_id != "any":
        where_or["tenant_id"] = tenant_id
        where_or["public"] = True
    vnf = mydb.get_table_by_uuid_name('vnfs', vnf_id, "VNF", WHERE_OR=where_or, WHERE_AND_OR="AND")
    vnf_id = vnf["uuid"]

    # "Getting the list of flavors and tenants of the VNF"
    flavorList = get_flavorlist(mydb, vnf_id)
    if len(flavorList)==0:
        logger.warn("delete_vnf error. No flavors found for the VNF id '%s'", vnf_id)

    imageList = get_imagelist(mydb, vnf_id)
    if len(imageList)==0:
        logger.warn( "delete_vnf error. No images found for the VNF id '%s'", vnf_id)

    deleted = mydb.delete_row_by_id('vnfs', vnf_id)
    if deleted == 0:
        raise NfvoException("vnf '{}' not found".format(vnf_id), httperrors.Not_Found)

    undeletedItems = []
    for flavor in flavorList:
        #check if flavor is used by other vnf
        try:
            c = mydb.get_rows(FROM='vms', WHERE={'flavor_id':flavor} )
            if len(c) > 0:
                logger.debug("Flavor '%s' not deleted because it is being used by another VNF", flavor)
                continue
            #flavor not used, must be deleted
            #delelte at VIM
            c = mydb.get_rows(FROM='datacenters_flavors', WHERE={'flavor_id': flavor})
            for flavor_vim in c:
                if not flavor_vim['created']:  # skip this flavor because not created by openmano
                    continue
                # look for vim
                myvim = None
                for vim in vims.values():
                    if vim["config"]["datacenter_tenant_id"] == flavor_vim["datacenter_vim_id"]:
                        myvim = vim
                        break
                if not myvim:
                    continue
                try:
                    myvim.delete_flavor(flavor_vim["vim_id"])
                except vimconn.vimconnNotFoundException:
                    logger.warn("VIM flavor %s not exist at datacenter %s", flavor_vim["vim_id"],
                                flavor_vim["datacenter_vim_id"] )
                except vimconn.vimconnException as e:
                    logger.error("Not possible to delete VIM flavor %s from datacenter %s: %s %s",
                            flavor_vim["vim_id"], flavor_vim["datacenter_vim_id"], type(e).__name__, str(e))
                    undeletedItems.append("flavor {} from VIM {}".format(flavor_vim["vim_id"],
                                                                         flavor_vim["datacenter_vim_id"]))
            # delete flavor from Database, using table flavors and with cascade foreign key also at datacenters_flavors
            mydb.delete_row_by_id('flavors', flavor)
        except db_base_Exception as e:
            logger.error("delete_vnf_error. Not possible to get flavor details and delete '%s'. %s", flavor, str(e))
            undeletedItems.append("flavor {}".format(flavor))


    for image in imageList:
        try:
            #check if image is used by other vnf
            c = mydb.get_rows(FROM='vms', WHERE=[{'image_id': image}, {'image_list LIKE ': '%' + image + '%'}])
            if len(c) > 0:
                logger.debug("Image '%s' not deleted because it is being used by another VNF", image)
                continue
            #image not used, must be deleted
            #delelte at VIM
            c = mydb.get_rows(FROM='datacenters_images', WHERE={'image_id':image})
            for image_vim in c:
                if image_vim["datacenter_vim_id"] not in vims:   # TODO change to datacenter_tenant_id
                    continue
                if image_vim['created']=='false': #skip this image because not created by openmano
                    continue
                myvim=vims[ image_vim["datacenter_id"] ]
                try:
                    myvim.delete_image(image_vim["vim_id"])
                except vimconn.vimconnNotFoundException as e:
                    logger.warn("VIM image %s not exist at datacenter %s", image_vim["vim_id"], image_vim["datacenter_id"] )
                except vimconn.vimconnException as e:
                    logger.error("Not possible to delete VIM image %s from datacenter %s: %s %s",
                            image_vim["vim_id"], image_vim["datacenter_id"], type(e).__name__, str(e))
                    undeletedItems.append("image {} from VIM {}".format(image_vim["vim_id"], image_vim["datacenter_id"] ))
            #delete image from Database, using table images and with cascade foreign key also at datacenters_images
            mydb.delete_row_by_id('images', image)
        except db_base_Exception as e:
            logger.error("delete_vnf_error. Not possible to get image details and delete '%s'. %s", image, str(e))
            undeletedItems.append("image %s" % image)

    return vnf_id + " " + vnf["name"]
    #if undeletedItems:
    #    return "delete_vnf. Undeleted: %s" %(undeletedItems)


@deprecated("Not used")
def get_hosts_info(mydb, nfvo_tenant_id, datacenter_name=None):
    result, vims = get_vim(mydb, nfvo_tenant_id, None, datacenter_name)
    if result < 0:
        return result, vims
    elif result == 0:
        return -httperrors.Not_Found, "datacenter '%s' not found" % datacenter_name
    myvim = vims.values()[0]
    result,servers =  myvim.get_hosts_info()
    if result < 0:
        return result, servers
    topology = {'name':myvim['name'] , 'servers': servers}
    return result, topology


def get_hosts(mydb, nfvo_tenant_id):
    vims = get_vim(mydb, nfvo_tenant_id)
    if len(vims) == 0:
        raise NfvoException("No datacenter found for tenant '{}'".format(str(nfvo_tenant_id)), httperrors.Not_Found)
    elif len(vims)>1:
        #print "nfvo.datacenter_action() error. Several datacenters found"
        raise NfvoException("More than one datacenters found, try to identify with uuid", httperrors.Conflict)
    myvim = vims.values()[0]
    try:
        hosts =  myvim.get_hosts()
        logger.debug('VIM hosts response: '+ yaml.safe_dump(hosts, indent=4, default_flow_style=False))

        datacenter = {'Datacenters': [ {'name':myvim['name'],'servers':[]} ] }
        for host in hosts:
            server={'name':host['name'], 'vms':[]}
            for vm in host['instances']:
                #get internal name and model
                try:
                    c = mydb.get_rows(SELECT=('name',), FROM='instance_vms as iv join vms on iv.vm_id=vms.uuid',\
                        WHERE={'vim_vm_id':vm['id']} )
                    if len(c) == 0:
                        logger.warn("nfvo.get_hosts virtual machine at VIM '{}' not found at tidnfvo".format(vm['id']))
                        continue
                    server['vms'].append( {'name':vm['name'] , 'model':c[0]['name']} )

                except db_base_Exception as e:
                    logger.warn("nfvo.get_hosts virtual machine at VIM '{}' error {}".format(vm['id'], str(e)))
            datacenter['Datacenters'][0]['servers'].append(server)
        #return -400, "en construccion"

        #print 'datacenters '+ json.dumps(datacenter, indent=4)
        return datacenter
    except vimconn.vimconnException as e:
        raise NfvoException("Not possible to get_host_list from VIM: {}".format(str(e)), e.http_code)


@deprecated("Use new_nsd_v3")
def new_scenario(mydb, tenant_id, topo):

#    result, vims = get_vim(mydb, tenant_id)
#    if result < 0:
#        return result, vims
#1: parse input
    if tenant_id != "any":
        check_tenant(mydb, tenant_id)
        if "tenant_id" in topo:
            if topo["tenant_id"] != tenant_id:
                raise NfvoException("VNF can not have a different tenant owner '{}', must be '{}'".format(topo["tenant_id"], tenant_id),
                                    httperrors.Unauthorized)
    else:
        tenant_id=None

#1.1: get VNFs and external_networks (other_nets).
    vnfs={}
    other_nets={}  #external_networks, bridge_networks and data_networkds
    nodes = topo['topology']['nodes']
    for k in nodes.keys():
        if nodes[k]['type'] == 'VNF':
            vnfs[k] = nodes[k]
            vnfs[k]['ifaces'] = {}
        elif nodes[k]['type'] == 'other_network' or nodes[k]['type'] == 'external_network':
            other_nets[k] = nodes[k]
            other_nets[k]['external']=True
        elif nodes[k]['type'] == 'network':
            other_nets[k] = nodes[k]
            other_nets[k]['external']=False


#1.2: Check that VNF are present at database table vnfs. Insert uuid, description and external interfaces
    for name,vnf in vnfs.items():
        where = {"OR": {"tenant_id": tenant_id, 'public': "true"}}
        error_text = ""
        error_pos = "'topology':'nodes':'" + name + "'"
        if 'vnf_id' in vnf:
            error_text += " 'vnf_id' " +  vnf['vnf_id']
            where['uuid'] = vnf['vnf_id']
        if 'VNF model' in vnf:
            error_text += " 'VNF model' " +  vnf['VNF model']
            where['name'] = vnf['VNF model']
        if len(where) == 1:
            raise NfvoException("Descriptor need a 'vnf_id' or 'VNF model' field at " + error_pos, httperrors.Bad_Request)

        vnf_db = mydb.get_rows(SELECT=('uuid','name','description'),
                               FROM='vnfs',
                               WHERE=where)
        if len(vnf_db)==0:
            raise NfvoException("unknown" + error_text + " at " + error_pos, httperrors.Not_Found)
        elif len(vnf_db)>1:
            raise NfvoException("more than one" + error_text + " at " + error_pos + " Concrete with 'vnf_id'", httperrors.Conflict)
        vnf['uuid']=vnf_db[0]['uuid']
        vnf['description']=vnf_db[0]['description']
        #get external interfaces
        ext_ifaces = mydb.get_rows(SELECT=('external_name as name','i.uuid as iface_uuid', 'i.type as type'),
            FROM='vnfs join vms on vnfs.uuid=vms.vnf_id join interfaces as i on vms.uuid=i.vm_id',
            WHERE={'vnfs.uuid':vnf['uuid'], 'external_name<>': None} )
        for ext_iface in ext_ifaces:
            vnf['ifaces'][ ext_iface['name'] ] = {'uuid':ext_iface['iface_uuid'], 'type':ext_iface['type']}

#1.4 get list of connections
    conections = topo['topology']['connections']
    conections_list = []
    conections_list_name = []
    for k in conections.keys():
        if type(conections[k]['nodes'])==dict: #dict with node:iface pairs
            ifaces_list = conections[k]['nodes'].items()
        elif type(conections[k]['nodes'])==list: #list with dictionary
            ifaces_list=[]
            conection_pair_list = map(lambda x: x.items(), conections[k]['nodes'] )
            for k2 in conection_pair_list:
                ifaces_list += k2

        con_type = conections[k].get("type", "link")
        if con_type != "link":
            if k in other_nets:
                raise NfvoException("Format error. Reapeted network name at 'topology':'connections':'{}'".format(str(k)), httperrors.Bad_Request)
            other_nets[k] = {'external': False}
            if conections[k].get("graph"):
                other_nets[k]["graph"] =   conections[k]["graph"]
            ifaces_list.append( (k, None) )


        if con_type == "external_network":
            other_nets[k]['external'] = True
            if conections[k].get("model"):
                other_nets[k]["model"] =   conections[k]["model"]
            else:
                other_nets[k]["model"] =   k
        if con_type == "dataplane_net" or con_type == "bridge_net":
            other_nets[k]["model"] = con_type

        conections_list_name.append(k)
        conections_list.append(set(ifaces_list)) #from list to set to operate as a set (this conversion removes elements that are repeated in a list)
        #print set(ifaces_list)
    #check valid VNF and iface names
        for iface in ifaces_list:
            if iface[0] not in vnfs and iface[0] not in other_nets :
                raise NfvoException("format error. Invalid VNF name at 'topology':'connections':'{}':'nodes':'{}'".format(
                                                                                        str(k), iface[0]), httperrors.Not_Found)
            if iface[0] in vnfs and iface[1] not in vnfs[ iface[0] ]['ifaces']:
                raise NfvoException("format error. Invalid interface name at 'topology':'connections':'{}':'nodes':'{}':'{}'".format(
                                                                                        str(k), iface[0], iface[1]), httperrors.Not_Found)

#1.5 unify connections from the pair list to a consolidated list
    index=0
    while index < len(conections_list):
        index2 = index+1
        while index2 < len(conections_list):
            if len(conections_list[index] & conections_list[index2])>0: #common interface, join nets
                conections_list[index] |= conections_list[index2]
                del conections_list[index2]
                del conections_list_name[index2]
            else:
                index2 += 1
        conections_list[index] = list(conections_list[index])  # from set to list again
        index += 1
    #for k in conections_list:
    #    print k



#1.6 Delete non external nets
#    for k in other_nets.keys():
#        if other_nets[k]['model']=='bridge' or other_nets[k]['model']=='dataplane_net' or other_nets[k]['model']=='bridge_net':
#            for con in conections_list:
#                delete_indexes=[]
#                for index in range(0,len(con)):
#                    if con[index][0] == k: delete_indexes.insert(0,index) #order from higher to lower
#                for index in delete_indexes:
#                    del con[index]
#            del other_nets[k]
#1.7: Check external_ports are present at database table datacenter_nets
    for k,net in other_nets.items():
        error_pos = "'topology':'nodes':'" + k + "'"
        if net['external']==False:
            if 'name' not in net:
                net['name']=k
            if 'model' not in net:
                raise NfvoException("needed a 'model' at " + error_pos, httperrors.Bad_Request)
            if net['model']=='bridge_net':
                net['type']='bridge';
            elif net['model']=='dataplane_net':
                net['type']='data';
            else:
                raise NfvoException("unknown 'model' '"+ net['model'] +"' at " + error_pos, httperrors.Not_Found)
        else: #external
#IF we do not want to check that external network exist at datacenter
            pass
#ELSE
#             error_text = ""
#             WHERE_={}
#             if 'net_id' in net:
#                 error_text += " 'net_id' " +  net['net_id']
#                 WHERE_['uuid'] = net['net_id']
#             if 'model' in net:
#                 error_text += " 'model' " +  net['model']
#                 WHERE_['name'] = net['model']
#             if len(WHERE_) == 0:
#                 return -httperrors.Bad_Request, "needed a 'net_id' or 'model' at " + error_pos
#             r,net_db = mydb.get_table(SELECT=('uuid','name','description','type','shared'),
#                 FROM='datacenter_nets', WHERE=WHERE_ )
#             if r<0:
#                 print "nfvo.new_scenario Error getting datacenter_nets",r,net_db
#             elif r==0:
#                 print "nfvo.new_scenario Error" +error_text+ " is not present at database"
#                 return -httperrors.Bad_Request, "unknown " +error_text+ " at " + error_pos
#             elif r>1:
#                 print "nfvo.new_scenario Error more than one external_network for " +error_text+ " is present at database"
#                 return -httperrors.Bad_Request, "more than one external_network for " +error_text+ "at "+ error_pos + " Concrete with 'net_id'"
#             other_nets[k].update(net_db[0])
#ENDIF
    net_list={}
    net_nb=0  #Number of nets
    for con in conections_list:
        #check if this is connected to a external net
        other_net_index=-1
        #print
        #print "con", con
        for index in range(0,len(con)):
            #check if this is connected to a external net
            for net_key in other_nets.keys():
                if con[index][0]==net_key:
                    if other_net_index>=0:
                        error_text="There is some interface connected both to net '%s' and net '%s'" % (con[other_net_index][0], net_key)
                        #print "nfvo.new_scenario " + error_text
                        raise NfvoException(error_text, httperrors.Bad_Request)
                    else:
                        other_net_index = index
                        net_target = net_key
                    break
        #print "other_net_index", other_net_index
        try:
            if other_net_index>=0:
                del con[other_net_index]
#IF we do not want to check that external network exist at datacenter
                if other_nets[net_target]['external'] :
                    if "name" not in other_nets[net_target]:
                        other_nets[net_target]['name'] =  other_nets[net_target]['model']
                    if other_nets[net_target]["type"] == "external_network":
                        if vnfs[ con[0][0] ]['ifaces'][ con[0][1] ]["type"] == "data":
                            other_nets[net_target]["type"] =  "data"
                        else:
                            other_nets[net_target]["type"] =  "bridge"
#ELSE
#                 if other_nets[net_target]['external'] :
#                     type_='data' if len(con)>1 else 'ptp'  #an external net is connected to a external port, so it is ptp if only one connection is done to this net
#                     if type_=='data' and other_nets[net_target]['type']=="ptp":
#                         error_text = "Error connecting %d nodes on a not multipoint net %s" % (len(con), net_target)
#                         print "nfvo.new_scenario " + error_text
#                         return -httperrors.Bad_Request, error_text
#ENDIF
                for iface in con:
                    vnfs[ iface[0] ]['ifaces'][ iface[1] ]['net_key'] = net_target
            else:
                #create a net
                net_type_bridge=False
                net_type_data=False
                net_target = "__-__net"+str(net_nb)
                net_list[net_target] = {'name': conections_list_name[net_nb],  #"net-"+str(net_nb),
                    'description':"net-%s in scenario %s" %(net_nb,topo['name']),
                    'external':False}
                for iface in con:
                    vnfs[ iface[0] ]['ifaces'][ iface[1] ]['net_key'] = net_target
                    iface_type = vnfs[ iface[0] ]['ifaces'][ iface[1] ]['type']
                    if iface_type=='mgmt' or iface_type=='bridge':
                        net_type_bridge = True
                    else:
                        net_type_data = True
                if net_type_bridge and net_type_data:
                    error_text = "Error connection interfaces of bridge type with data type. Firs node %s, iface %s" % (iface[0], iface[1])
                    #print "nfvo.new_scenario " + error_text
                    raise NfvoException(error_text, httperrors.Bad_Request)
                elif net_type_bridge:
                    type_='bridge'
                else:
                    type_='data' if len(con)>2 else 'ptp'
                net_list[net_target]['type'] = type_
                net_nb+=1
        except Exception:
            error_text = "Error connection node %s : %s does not match any VNF or interface" % (iface[0], iface[1])
            #print "nfvo.new_scenario " + error_text
            #raise e
            raise NfvoException(error_text, httperrors.Bad_Request)

#1.8: Connect to management net all not already connected interfaces of type 'mgmt'
    #1.8.1 obtain management net
    mgmt_net = mydb.get_rows(SELECT=('uuid','name','description','type','shared'),
        FROM='datacenter_nets', WHERE={'name':'mgmt'} )
    #1.8.2 check all interfaces from all vnfs
    if len(mgmt_net)>0:
        add_mgmt_net = False
        for vnf in vnfs.values():
            for iface in vnf['ifaces'].values():
                if iface['type']=='mgmt' and 'net_key' not in iface:
                    #iface not connected
                    iface['net_key'] = 'mgmt'
                    add_mgmt_net = True
        if add_mgmt_net and 'mgmt' not in net_list:
            net_list['mgmt']=mgmt_net[0]
            net_list['mgmt']['external']=True
            net_list['mgmt']['graph']={'visible':False}

    net_list.update(other_nets)
    #print
    #print 'net_list', net_list
    #print
    #print 'vnfs', vnfs
    #print

#2: insert scenario. filling tables scenarios,sce_vnfs,sce_interfaces,sce_nets
    c = mydb.new_scenario( { 'vnfs':vnfs, 'nets':net_list,
        'tenant_id':tenant_id, 'name':topo['name'],
         'description':topo.get('description',topo['name']),
         'public': topo.get('public', False)
         })

    return c


@deprecated("Use new_nsd_v3")
def new_scenario_v02(mydb, tenant_id, scenario_dict, version):
    """ This creates a new scenario for version 0.2 and 0.3"""
    scenario = scenario_dict["scenario"]
    if tenant_id != "any":
        check_tenant(mydb, tenant_id)
        if "tenant_id" in scenario:
            if scenario["tenant_id"] != tenant_id:
                # print "nfvo.new_scenario_v02() tenant '%s' not found" % tenant_id
                raise NfvoException("VNF can not have a different tenant owner '{}', must be '{}'".format(
                                                    scenario["tenant_id"], tenant_id), httperrors.Unauthorized)
    else:
        tenant_id=None

    # 1: Check that VNF are present at database table vnfs and update content into scenario dict
    for name,vnf in scenario["vnfs"].iteritems():
        where = {"OR": {"tenant_id": tenant_id, 'public': "true"}}
        error_text = ""
        error_pos = "'scenario':'vnfs':'" + name + "'"
        if 'vnf_id' in vnf:
            error_text += " 'vnf_id' " + vnf['vnf_id']
            where['uuid'] = vnf['vnf_id']
        if 'vnf_name' in vnf:
            error_text += " 'vnf_name' " + vnf['vnf_name']
            where['name'] = vnf['vnf_name']
        if len(where) == 1:
            raise NfvoException("Needed a 'vnf_id' or 'vnf_name' at " + error_pos, httperrors.Bad_Request)
        vnf_db = mydb.get_rows(SELECT=('uuid', 'name', 'description'),
                               FROM='vnfs',
                               WHERE=where)
        if len(vnf_db) == 0:
            raise NfvoException("Unknown" + error_text + " at " + error_pos, httperrors.Not_Found)
        elif len(vnf_db) > 1:
            raise NfvoException("More than one" + error_text + " at " + error_pos + " Concrete with 'vnf_id'", httperrors.Conflict)
        vnf['uuid'] = vnf_db[0]['uuid']
        vnf['description'] = vnf_db[0]['description']
        vnf['ifaces'] = {}
        # get external interfaces
        ext_ifaces = mydb.get_rows(SELECT=('external_name as name', 'i.uuid as iface_uuid', 'i.type as type'),
                                   FROM='vnfs join vms on vnfs.uuid=vms.vnf_id join interfaces as i on vms.uuid=i.vm_id',
                                   WHERE={'vnfs.uuid':vnf['uuid'], 'external_name<>': None} )
        for ext_iface in ext_ifaces:
            vnf['ifaces'][ ext_iface['name'] ] = {'uuid':ext_iface['iface_uuid'], 'type': ext_iface['type']}
        # TODO? get internal-connections from db.nets and their profiles, and update scenario[vnfs][internal-connections] accordingly

    # 2: Insert net_key and ip_address at every vnf interface
    for net_name, net in scenario["networks"].items():
        net_type_bridge = False
        net_type_data = False
        for iface_dict in net["interfaces"]:
            if version == "0.2":
                temp_dict = iface_dict
                ip_address = None
            elif version == "0.3":
                temp_dict = {iface_dict["vnf"] : iface_dict["vnf_interface"]}
                ip_address = iface_dict.get('ip_address', None)
            for vnf, iface in temp_dict.items():
                if vnf not in scenario["vnfs"]:
                    error_text = "Error at 'networks':'{}':'interfaces' VNF '{}' not match any VNF at 'vnfs'".format(
                        net_name, vnf)
                    # logger.debug("nfvo.new_scenario_v02 " + error_text)
                    raise NfvoException(error_text, httperrors.Not_Found)
                if iface not in scenario["vnfs"][vnf]['ifaces']:
                    error_text = "Error at 'networks':'{}':'interfaces':'{}' interface not match any VNF interface"\
                        .format(net_name, iface)
                    # logger.debug("nfvo.new_scenario_v02 " + error_text)
                    raise NfvoException(error_text, httperrors.Bad_Request)
                if "net_key" in scenario["vnfs"][vnf]['ifaces'][iface]:
                    error_text = "Error at 'networks':'{}':'interfaces':'{}' interface already connected at network"\
                                 "'{}'".format(net_name, iface,scenario["vnfs"][vnf]['ifaces'][iface]['net_key'])
                    # logger.debug("nfvo.new_scenario_v02 " + error_text)
                    raise NfvoException(error_text, httperrors.Bad_Request)
                scenario["vnfs"][vnf]['ifaces'][ iface ]['net_key'] = net_name
                scenario["vnfs"][vnf]['ifaces'][iface]['ip_address'] = ip_address
                iface_type = scenario["vnfs"][vnf]['ifaces'][iface]['type']
                if iface_type == 'mgmt' or iface_type == 'bridge':
                    net_type_bridge = True
                else:
                    net_type_data = True

        if net_type_bridge and net_type_data:
            error_text = "Error connection interfaces of 'bridge' type and 'data' type at 'networks':'{}':'interfaces'"\
                .format(net_name)
            # logger.debug("nfvo.new_scenario " + error_text)
            raise NfvoException(error_text, httperrors.Bad_Request)
        elif net_type_bridge:
            type_ = 'bridge'
        else:
            type_ = 'data' if len(net["interfaces"]) > 2 else 'ptp'

        if net.get("implementation"):     # for v0.3
            if type_ == "bridge" and net["implementation"] == "underlay":
                error_text = "Error connecting interfaces of data type to a network declared as 'underlay' at "\
                             "'network':'{}'".format(net_name)
                # logger.debug(error_text)
                raise NfvoException(error_text, httperrors.Bad_Request)
            elif type_ != "bridge" and net["implementation"] == "overlay":
                error_text = "Error connecting interfaces of data type to a network declared as 'overlay' at "\
                             "'network':'{}'".format(net_name)
                # logger.debug(error_text)
                raise NfvoException(error_text, httperrors.Bad_Request)
            net.pop("implementation")
        if "type" in net and version == "0.3":   # for v0.3
            if type_ == "data" and net["type"] == "e-line":
                error_text = "Error connecting more than 2 interfaces of data type to a network declared as type "\
                             "'e-line' at 'network':'{}'".format(net_name)
                # logger.debug(error_text)
                raise NfvoException(error_text, httperrors.Bad_Request)
            elif type_ == "ptp" and net["type"] == "e-lan":
                type_ = "data"

        net['type'] = type_
        net['name'] = net_name
        net['external'] = net.get('external', False)

    # 3: insert at database
    scenario["nets"] = scenario["networks"]
    scenario['tenant_id'] = tenant_id
    scenario_id = mydb.new_scenario(scenario)
    return scenario_id


def new_nsd_v3(mydb, tenant_id, nsd_descriptor):
    """
    Parses an OSM IM nsd_catalog and insert at DB
    :param mydb:
    :param tenant_id:
    :param nsd_descriptor:
    :return: The list of created NSD ids
    """
    try:
        mynsd = nsd_catalog.nsd()
        try:
            pybindJSONDecoder.load_ietf_json(nsd_descriptor, None, None, obj=mynsd, skip_unknown=True)
        except Exception as e:
            raise NfvoException("Error. Invalid NS descriptor format: " + str(e), httperrors.Bad_Request)
        db_scenarios = []
        db_sce_nets = []
        db_sce_vnfs = []
        db_sce_interfaces = []
        db_sce_vnffgs = []
        db_sce_rsps = []
        db_sce_rsp_hops = []
        db_sce_classifiers = []
        db_sce_classifier_matches = []
        db_ip_profiles = []
        db_ip_profiles_index = 0
        uuid_list = []
        nsd_uuid_list = []
        for nsd_yang in mynsd.nsd_catalog.nsd.itervalues():
            nsd = nsd_yang.get()

            # table scenarios
            scenario_uuid = str(uuid4())
            uuid_list.append(scenario_uuid)
            nsd_uuid_list.append(scenario_uuid)
            db_scenario = {
                "uuid": scenario_uuid,
                "osm_id": get_str(nsd, "id", 255),
                "name": get_str(nsd, "name", 255),
                "description": get_str(nsd, "description", 255),
                "tenant_id": tenant_id,
                "vendor": get_str(nsd, "vendor", 255),
                "short_name": get_str(nsd, "short-name", 255),
                "descriptor": str(nsd_descriptor)[:60000],
            }
            db_scenarios.append(db_scenario)

            # table sce_vnfs (constituent-vnfd)
            vnf_index2scevnf_uuid = {}
            vnf_index2vnf_uuid = {}
            for vnf in nsd.get("constituent-vnfd").itervalues():
                existing_vnf = mydb.get_rows(FROM="vnfs", WHERE={'osm_id': str(vnf["vnfd-id-ref"])[:255],
                                                                      'tenant_id': tenant_id})
                if not existing_vnf:
                    raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'constituent-vnfd':'vnfd-id-ref':"
                                        "'{}'. Reference to a non-existing VNFD in the catalog".format(
                                            str(nsd["id"]), str(vnf["vnfd-id-ref"])[:255]),
                                        httperrors.Bad_Request)
                sce_vnf_uuid = str(uuid4())
                uuid_list.append(sce_vnf_uuid)
                db_sce_vnf = {
                    "uuid": sce_vnf_uuid,
                    "scenario_id": scenario_uuid,
                    # "name": get_str(vnf, "member-vnf-index", 255),
                    "name": existing_vnf[0]["name"][:200] + "." + get_str(vnf, "member-vnf-index", 50),
                    "vnf_id": existing_vnf[0]["uuid"],
                    "member_vnf_index": str(vnf["member-vnf-index"]),
                    # TODO 'start-by-default': True
                }
                vnf_index2scevnf_uuid[str(vnf['member-vnf-index'])] = sce_vnf_uuid
                vnf_index2vnf_uuid[str(vnf['member-vnf-index'])] = existing_vnf[0]["uuid"]
                db_sce_vnfs.append(db_sce_vnf)

            # table ip_profiles (ip-profiles)
            ip_profile_name2db_table_index = {}
            for ip_profile in nsd.get("ip-profiles").itervalues():
                db_ip_profile = {
                    "ip_version": str(ip_profile["ip-profile-params"].get("ip-version", "ipv4")),
                    "subnet_address": str(ip_profile["ip-profile-params"].get("subnet-address")),
                    "gateway_address": str(ip_profile["ip-profile-params"].get("gateway-address")),
                    "dhcp_enabled": str(ip_profile["ip-profile-params"]["dhcp-params"].get("enabled", True)),
                    "dhcp_start_address": str(ip_profile["ip-profile-params"]["dhcp-params"].get("start-address")),
                    "dhcp_count": str(ip_profile["ip-profile-params"]["dhcp-params"].get("count")),
                }
                dns_list = []
                for dns in ip_profile["ip-profile-params"]["dns-server"].itervalues():
                    dns_list.append(str(dns.get("address")))
                db_ip_profile["dns_address"] = ";".join(dns_list)
                if ip_profile["ip-profile-params"].get('security-group'):
                    db_ip_profile["security_group"] = ip_profile["ip-profile-params"]['security-group']
                ip_profile_name2db_table_index[str(ip_profile["name"])] = db_ip_profiles_index
                db_ip_profiles_index += 1
                db_ip_profiles.append(db_ip_profile)

            # table sce_nets (internal-vld)
            for vld in nsd.get("vld").itervalues():
                sce_net_uuid = str(uuid4())
                uuid_list.append(sce_net_uuid)
                db_sce_net = {
                    "uuid": sce_net_uuid,
                    "name": get_str(vld, "name", 255),
                    "scenario_id": scenario_uuid,
                    # "type": #TODO
                    "multipoint": not vld.get("type") == "ELINE",
                    "osm_id":  get_str(vld, "id", 255),
                    # "external": #TODO
                    "description": get_str(vld, "description", 255),
                }
                # guess type of network
                if vld.get("mgmt-network"):
                    db_sce_net["type"] = "bridge"
                    db_sce_net["external"] = True
                elif vld.get("provider-network").get("overlay-type") == "VLAN":
                    db_sce_net["type"] = "data"
                else:
                    # later on it will be fixed to bridge or data depending on the type of interfaces attached to it
                    db_sce_net["type"] = None
                db_sce_nets.append(db_sce_net)

                # ip-profile, link db_ip_profile with db_sce_net
                if vld.get("ip-profile-ref"):
                    ip_profile_name = vld.get("ip-profile-ref")
                    if ip_profile_name not in ip_profile_name2db_table_index:
                        raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'vld[{}]':'ip-profile-ref':'{}'."
                                            " Reference to a non-existing 'ip_profiles'".format(
                                                str(nsd["id"]), str(vld["id"]), str(vld["ip-profile-ref"])),
                                            httperrors.Bad_Request)
                    db_ip_profiles[ip_profile_name2db_table_index[ip_profile_name]]["sce_net_id"] = sce_net_uuid
                elif vld.get("vim-network-name"):
                    db_sce_net["vim_network_name"] = get_str(vld, "vim-network-name", 255)

                # table sce_interfaces (vld:vnfd-connection-point-ref)
                for iface in vld.get("vnfd-connection-point-ref").itervalues():
                    vnf_index = str(iface['member-vnf-index-ref'])
                    # check correct parameters
                    if vnf_index not in vnf_index2vnf_uuid:
                        raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'vld[{}]':'vnfd-connection-point"
                                            "-ref':'member-vnf-index-ref':'{}'. Reference to a non-existing index at "
                                            "'nsd':'constituent-vnfd'".format(
                                                str(nsd["id"]), str(vld["id"]), str(iface["member-vnf-index-ref"])),
                                            httperrors.Bad_Request)

                    existing_ifaces = mydb.get_rows(SELECT=('i.uuid as uuid', 'i.type as iface_type'),
                                                    FROM="interfaces as i join vms on i.vm_id=vms.uuid",
                                                    WHERE={'vnf_id': vnf_index2vnf_uuid[vnf_index],
                                                           'external_name': get_str(iface, "vnfd-connection-point-ref",
                                                                                    255)})
                    if not existing_ifaces:
                        raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'vld[{}]':'vnfd-connection-point"
                                            "-ref':'vnfd-connection-point-ref':'{}'. Reference to a non-existing "
                                            "connection-point name at VNFD '{}'".format(
                                                str(nsd["id"]), str(vld["id"]), str(iface["vnfd-connection-point-ref"]),
                                                str(iface.get("vnfd-id-ref"))[:255]),
                                            httperrors.Bad_Request)
                    interface_uuid = existing_ifaces[0]["uuid"]
                    if existing_ifaces[0]["iface_type"] == "data":
                        db_sce_net["type"] = "data"
                    sce_interface_uuid = str(uuid4())
                    uuid_list.append(sce_net_uuid)
                    iface_ip_address = None
                    if iface.get("ip-address"):
                        iface_ip_address = str(iface.get("ip-address"))
                    db_sce_interface = {
                        "uuid": sce_interface_uuid,
                        "sce_vnf_id": vnf_index2scevnf_uuid[vnf_index],
                        "sce_net_id": sce_net_uuid,
                        "interface_id": interface_uuid,
                        "ip_address": iface_ip_address,
                    }
                    db_sce_interfaces.append(db_sce_interface)
                if not db_sce_net["type"]:
                    db_sce_net["type"] = "bridge"

            # table sce_vnffgs (vnffgd)
            for vnffg in nsd.get("vnffgd").itervalues():
                sce_vnffg_uuid = str(uuid4())
                uuid_list.append(sce_vnffg_uuid)
                db_sce_vnffg = {
                    "uuid": sce_vnffg_uuid,
                    "name": get_str(vnffg, "name", 255),
                    "scenario_id": scenario_uuid,
                    "vendor": get_str(vnffg, "vendor", 255),
                    "description": get_str(vld, "description", 255),
                }
                db_sce_vnffgs.append(db_sce_vnffg)

                # deal with rsps
                for rsp in vnffg.get("rsp").itervalues():
                    sce_rsp_uuid = str(uuid4())
                    uuid_list.append(sce_rsp_uuid)
                    db_sce_rsp = {
                        "uuid": sce_rsp_uuid,
                        "name": get_str(rsp, "name", 255),
                        "sce_vnffg_id": sce_vnffg_uuid,
                        "id": get_str(rsp, "id", 255), # only useful to link with classifiers; will be removed later in the code
                    }
                    db_sce_rsps.append(db_sce_rsp)
                    for iface in rsp.get("vnfd-connection-point-ref").itervalues():
                        vnf_index = str(iface['member-vnf-index-ref'])
                        if_order = int(iface['order'])
                        # check correct parameters
                        if vnf_index not in vnf_index2vnf_uuid:
                            raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'rsp[{}]':'vnfd-connection-point"
                                                "-ref':'member-vnf-index-ref':'{}'. Reference to a non-existing index at "
                                                "'nsd':'constituent-vnfd'".format(
                                                    str(nsd["id"]), str(rsp["id"]), str(iface["member-vnf-index-ref"])),
                                                httperrors.Bad_Request)

                        ingress_existing_ifaces = mydb.get_rows(SELECT=('i.uuid as uuid',),
                                                                FROM="interfaces as i join vms on i.vm_id=vms.uuid",
                                                                WHERE={
                                                                    'vnf_id': vnf_index2vnf_uuid[vnf_index],
                                                                    'external_name': get_str(iface, "vnfd-ingress-connection-point-ref",
                                                                                             255)})
                        if not ingress_existing_ifaces:
                            raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'rsp[{}]':'vnfd-connection-point"
                                                "-ref':'vnfd-ingress-connection-point-ref':'{}'. Reference to a non-existing "
                                                "connection-point name at VNFD '{}'".format(
                                str(nsd["id"]), str(rsp["id"]), str(iface["vnfd-ingress-connection-point-ref"]),
                                str(iface.get("vnfd-id-ref"))[:255]), httperrors.Bad_Request)

                        egress_existing_ifaces = mydb.get_rows(SELECT=('i.uuid as uuid',),
                                                               FROM="interfaces as i join vms on i.vm_id=vms.uuid",
                                                               WHERE={
                                                                   'vnf_id': vnf_index2vnf_uuid[vnf_index],
                                                                   'external_name': get_str(iface, "vnfd-egress-connection-point-ref",
                                                                                            255)})
                        if not egress_existing_ifaces:
                            raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'rsp[{}]':'vnfd-connection-point"
                                                "-ref':'vnfd-egress-connection-point-ref':'{}'. Reference to a non-existing "
                                                "connection-point name at VNFD '{}'".format(
                                str(nsd["id"]), str(rsp["id"]), str(iface["vnfd-egress-connection-point-ref"]),
                                str(iface.get("vnfd-id-ref"))[:255]), HTTP_Bad_Request)

                        ingress_interface_uuid = ingress_existing_ifaces[0]["uuid"]
                        egress_interface_uuid = egress_existing_ifaces[0]["uuid"]
                        sce_rsp_hop_uuid = str(uuid4())
                        uuid_list.append(sce_rsp_hop_uuid)
                        db_sce_rsp_hop = {
                            "uuid": sce_rsp_hop_uuid,
                            "if_order": if_order,
                            "ingress_interface_id": ingress_interface_uuid,
                            "egress_interface_id": egress_interface_uuid,
                            "sce_vnf_id": vnf_index2scevnf_uuid[vnf_index],
                            "sce_rsp_id": sce_rsp_uuid,
                        }
                        db_sce_rsp_hops.append(db_sce_rsp_hop)

                # deal with classifiers
                for classifier in vnffg.get("classifier").itervalues():
                    sce_classifier_uuid = str(uuid4())
                    uuid_list.append(sce_classifier_uuid)

                    # source VNF
                    vnf_index = str(classifier['member-vnf-index-ref'])
                    if vnf_index not in vnf_index2vnf_uuid:
                        raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'classifier[{}]':'vnfd-connection-point"
                                            "-ref':'member-vnf-index-ref':'{}'. Reference to a non-existing index at "
                                            "'nsd':'constituent-vnfd'".format(
                                                str(nsd["id"]), str(classifier["id"]), str(classifier["member-vnf-index-ref"])),
                                            httperrors.Bad_Request)
                    existing_ifaces = mydb.get_rows(SELECT=('i.uuid as uuid',),
                                                    FROM="interfaces as i join vms on i.vm_id=vms.uuid",
                                                    WHERE={'vnf_id': vnf_index2vnf_uuid[vnf_index],
                                                           'external_name': get_str(classifier, "vnfd-connection-point-ref",
                                                                                    255)})
                    if not existing_ifaces:
                        raise NfvoException("Error. Invalid NS descriptor at 'nsd[{}]':'rsp[{}]':'vnfd-connection-point"
                                            "-ref':'vnfd-connection-point-ref':'{}'. Reference to a non-existing "
                                            "connection-point name at VNFD '{}'".format(
                                                str(nsd["id"]), str(rsp["id"]), str(iface["vnfd-connection-point-ref"]),
                                                str(iface.get("vnfd-id-ref"))[:255]),
                                            httperrors.Bad_Request)
                    interface_uuid = existing_ifaces[0]["uuid"]

                    db_sce_classifier = {
                        "uuid": sce_classifier_uuid,
                        "name": get_str(classifier, "name", 255),
                        "sce_vnffg_id": sce_vnffg_uuid,
                        "sce_vnf_id": vnf_index2scevnf_uuid[vnf_index],
                        "interface_id": interface_uuid,
                    }
                    rsp_id = get_str(classifier, "rsp-id-ref", 255)
                    rsp = next((item for item in db_sce_rsps if item["id"] == rsp_id), None)
                    db_sce_classifier["sce_rsp_id"] = rsp["uuid"]
                    db_sce_classifiers.append(db_sce_classifier)

                    for match in classifier.get("match-attributes").itervalues():
                        sce_classifier_match_uuid = str(uuid4())
                        uuid_list.append(sce_classifier_match_uuid)
                        db_sce_classifier_match = {
                            "uuid": sce_classifier_match_uuid,
                            "ip_proto": get_str(match, "ip-proto", 2),
                            "source_ip": get_str(match, "source-ip-address", 16),
                            "destination_ip": get_str(match, "destination-ip-address", 16),
                            "source_port": get_str(match, "source-port", 5),
                            "destination_port": get_str(match, "destination-port", 5),
                            "sce_classifier_id": sce_classifier_uuid,
                        }
                        db_sce_classifier_matches.append(db_sce_classifier_match)
                    # TODO: vnf/cp keys

        # remove unneeded id's in sce_rsps
        for rsp in db_sce_rsps:
            rsp.pop('id')

        db_tables = [
            {"scenarios": db_scenarios},
            {"sce_nets": db_sce_nets},
            {"ip_profiles": db_ip_profiles},
            {"sce_vnfs": db_sce_vnfs},
            {"sce_interfaces": db_sce_interfaces},
            {"sce_vnffgs": db_sce_vnffgs},
            {"sce_rsps": db_sce_rsps},
            {"sce_rsp_hops": db_sce_rsp_hops},
            {"sce_classifiers": db_sce_classifiers},
            {"sce_classifier_matches": db_sce_classifier_matches},
        ]

        logger.debug("new_nsd_v3 done: %s",
                    yaml.safe_dump(db_tables, indent=4, default_flow_style=False) )
        mydb.new_rows(db_tables, uuid_list)
        return nsd_uuid_list
    except NfvoException:
        raise
    except Exception as e:
        logger.error("Exception {}".format(e))
        raise  # NfvoException("Exception {}".format(e), httperrors.Bad_Request)


def edit_scenario(mydb, tenant_id, scenario_id, data):
    data["uuid"] = scenario_id
    data["tenant_id"] = tenant_id
    c = mydb.edit_scenario( data )
    return c


@deprecated("Use create_instance")
def start_scenario(mydb, tenant_id, scenario_id, instance_scenario_name, instance_scenario_description, datacenter=None,vim_tenant=None, startvms=True):
    #print "Checking that nfvo_tenant_id exists and getting the VIM URI and the VIM tenant_id"
    datacenter_id, myvim = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter, vim_tenant=vim_tenant)
    vims = {datacenter_id: myvim}
    myvim_tenant = myvim['tenant_id']
    datacenter_name = myvim['name']

    rollbackList=[]
    try:
        #print "Checking that the scenario_id exists and getting the scenario dictionary"
        scenarioDict = mydb.get_scenario(scenario_id, tenant_id, datacenter_id=datacenter_id)
        scenarioDict['datacenter2tenant'] = { datacenter_id: myvim['config']['datacenter_tenant_id'] }
        scenarioDict['datacenter_id'] = datacenter_id
        #print '================scenarioDict======================='
        #print json.dumps(scenarioDict, indent=4)
        #print 'BEGIN launching instance scenario "%s" based on "%s"' % (instance_scenario_name,scenarioDict['name'])

        logger.debug("start_scenario Scenario %s: consisting of %d VNF(s)", scenarioDict['name'],len(scenarioDict['vnfs']))
        #print yaml.safe_dump(scenarioDict, indent=4, default_flow_style=False)

        auxNetDict = {}   #Auxiliar dictionary. First key:'scenario' or sce_vnf uuid. Second Key: uuid of the net/sce_net. Value: vim_net_id
        auxNetDict['scenario'] = {}

        logger.debug("start_scenario 1. Creating new nets (sce_nets) in the VIM")
        for sce_net in scenarioDict['nets']:
            #print "Net name: %s. Description: %s" % (sce_net["name"], sce_net["description"])

            myNetName = "%s.%s" % (instance_scenario_name, sce_net['name'])
            myNetName = myNetName[0:255] #limit length
            myNetType = sce_net['type']
            myNetDict = {}
            myNetDict["name"] = myNetName
            myNetDict["type"] = myNetType
            myNetDict["tenant_id"] = myvim_tenant
            myNetIPProfile = sce_net.get('ip_profile', None)
            #TODO:
            #We should use the dictionary as input parameter for new_network
            #print myNetDict
            if not sce_net["external"]:
                network_id, _ = myvim.new_network(myNetName, myNetType, myNetIPProfile)
                #print "New VIM network created for scenario %s. Network id:  %s" % (scenarioDict['name'],network_id)
                sce_net['vim_id'] = network_id
                auxNetDict['scenario'][sce_net['uuid']] = network_id
                rollbackList.append({'what':'network','where':'vim','vim_id':datacenter_id,'uuid':network_id})
                sce_net["created"] = True
            else:
                if sce_net['vim_id'] == None:
                    error_text = "Error, datacenter '%s' does not have external network '%s'." % (datacenter_name, sce_net['name'])
                    _, message = rollback(mydb, vims, rollbackList)
                    logger.error("nfvo.start_scenario: %s", error_text)
                    raise NfvoException(error_text, httperrors.Bad_Request)
                logger.debug("Using existent VIM network for scenario %s. Network id %s", scenarioDict['name'],sce_net['vim_id'])
                auxNetDict['scenario'][sce_net['uuid']] = sce_net['vim_id']

        logger.debug("start_scenario 2. Creating new nets (vnf internal nets) in the VIM")
        #For each vnf net, we create it and we add it to instanceNetlist.

        for sce_vnf in scenarioDict['vnfs']:
            for net in sce_vnf['nets']:
                #print "Net name: %s. Description: %s" % (net["name"], net["description"])

                myNetName = "%s.%s" % (instance_scenario_name,net['name'])
                myNetName = myNetName[0:255] #limit length
                myNetType = net['type']
                myNetDict = {}
                myNetDict["name"] = myNetName
                myNetDict["type"] = myNetType
                myNetDict["tenant_id"] = myvim_tenant
                myNetIPProfile = net.get('ip_profile', None)
                #print myNetDict
                #TODO:
                #We should use the dictionary as input parameter for new_network
                network_id, _  = myvim.new_network(myNetName, myNetType, myNetIPProfile)
                #print "VIM network id for scenario %s: %s" % (scenarioDict['name'],network_id)
                net['vim_id'] = network_id
                if sce_vnf['uuid'] not in auxNetDict:
                    auxNetDict[sce_vnf['uuid']] = {}
                auxNetDict[sce_vnf['uuid']][net['uuid']] = network_id
                rollbackList.append({'what':'network','where':'vim','vim_id':datacenter_id,'uuid':network_id})
                net["created"] = True

        #print "auxNetDict:"
        #print yaml.safe_dump(auxNetDict, indent=4, default_flow_style=False)

        logger.debug("start_scenario 3. Creating new vm instances in the VIM")
        #myvim.new_vminstance(self,vimURI,tenant_id,name,description,image_id,flavor_id,net_dict)
        i = 0
        for sce_vnf in scenarioDict['vnfs']:
            vnf_availability_zones = []
            for vm in sce_vnf['vms']:
                vm_av = vm.get('availability_zone')
                if vm_av and vm_av not in vnf_availability_zones:
                    vnf_availability_zones.append(vm_av)

            # check if there is enough availability zones available at vim level.
            if myvims[datacenter_id].availability_zone and vnf_availability_zones:
                if len(vnf_availability_zones) > len(myvims[datacenter_id].availability_zone):
                    raise NfvoException('No enough availability zones at VIM for this deployment', httperrors.Bad_Request)

            for vm in sce_vnf['vms']:
                i += 1
                myVMDict = {}
                #myVMDict['name'] = "%s-%s-%s" % (scenarioDict['name'],sce_vnf['name'], vm['name'])
                myVMDict['name'] = "{}.{}.{}".format(instance_scenario_name,sce_vnf['name'],chr(96+i))
                #myVMDict['description'] = vm['description']
                myVMDict['description'] = myVMDict['name'][0:99]
                if not startvms:
                    myVMDict['start'] = "no"
                myVMDict['name'] = myVMDict['name'][0:255] #limit name length
                #print "VM name: %s. Description: %s" % (myVMDict['name'], myVMDict['name'])

                #create image at vim in case it not exist
                image_dict = mydb.get_table_by_uuid_name("images", vm['image_id'])
                image_id = create_or_use_image(mydb, vims, image_dict, [], True)
                vm['vim_image_id'] = image_id

                #create flavor at vim in case it not exist
                flavor_dict = mydb.get_table_by_uuid_name("flavors", vm['flavor_id'])
                if flavor_dict['extended']!=None:
                    flavor_dict['extended']= yaml.load(flavor_dict['extended'])
                flavor_id = create_or_use_flavor(mydb, vims, flavor_dict, [], True)
                vm['vim_flavor_id'] = flavor_id


                myVMDict['imageRef'] = vm['vim_image_id']
                myVMDict['flavorRef'] = vm['vim_flavor_id']
                myVMDict['networks'] = []
                for iface in vm['interfaces']:
                    netDict = {}
                    if iface['type']=="data":
                        netDict['type'] = iface['model']
                    elif "model" in iface and iface["model"]!=None:
                        netDict['model']=iface['model']
                    #TODO in future, remove this because mac_address will not be set, and the type of PV,VF is obtained from iterface table model
                    #discover type of interface looking at flavor
                    for numa in flavor_dict.get('extended',{}).get('numas',[]):
                        for flavor_iface in numa.get('interfaces',[]):
                            if flavor_iface.get('name') == iface['internal_name']:
                                if flavor_iface['dedicated'] == 'yes':
                                    netDict['type']="PF"    #passthrough
                                elif flavor_iface['dedicated'] == 'no':
                                    netDict['type']="VF"    #siov
                                elif flavor_iface['dedicated'] == 'yes:sriov':
                                    netDict['type']="VFnotShared"   #sriov but only one sriov on the PF
                                netDict["mac_address"] = flavor_iface.get("mac_address")
                                break;
                    netDict["use"]=iface['type']
                    if netDict["use"]=="data" and not netDict.get("type"):
                        #print "netDict", netDict
                        #print "iface", iface
                        e_text = "Cannot determine the interface type PF or VF of VNF '%s' VM '%s' iface '%s'" %(sce_vnf['name'], vm['name'], iface['internal_name'])
                        if flavor_dict.get('extended')==None:
                            raise NfvoException(e_text  + "After database migration some information is not available. \
                                    Try to delete and create the scenarios and VNFs again", httperrors.Conflict)
                        else:
                            raise NfvoException(e_text, httperrors.Internal_Server_Error)
                    if netDict["use"]=="mgmt" or netDict["use"]=="bridge":
                        netDict["type"]="virtual"
                    if "vpci" in iface and iface["vpci"] is not None:
                        netDict['vpci'] = iface['vpci']
                    if "mac" in iface and iface["mac"] is not None:
                        netDict['mac_address'] = iface['mac']
                    if "port-security" in iface and iface["port-security"] is not None:
                        netDict['port_security'] = iface['port-security']
                    if "floating-ip" in iface and iface["floating-ip"] is not None:
                        netDict['floating_ip'] = iface['floating-ip']
                    netDict['name'] = iface['internal_name']
                    if iface['net_id'] is None:
                        for vnf_iface in sce_vnf["interfaces"]:
                            #print iface
                            #print vnf_iface
                            if vnf_iface['interface_id']==iface['uuid']:
                                netDict['net_id'] = auxNetDict['scenario'][ vnf_iface['sce_net_id'] ]
                                break
                    else:
                        netDict['net_id'] = auxNetDict[ sce_vnf['uuid'] ][ iface['net_id'] ]
                    #skip bridge ifaces not connected to any net
                    #if 'net_id' not in netDict or netDict['net_id']==None:
                    #    continue
                    myVMDict['networks'].append(netDict)
                #print ">>>>>>>>>>>>>>>>>>>>>>>>>>>"
                #print myVMDict['name']
                #print "networks", yaml.safe_dump(myVMDict['networks'], indent=4, default_flow_style=False)
                #print "interfaces", yaml.safe_dump(vm['interfaces'], indent=4, default_flow_style=False)
                #print ">>>>>>>>>>>>>>>>>>>>>>>>>>>"

                if 'availability_zone' in myVMDict:
                    av_index = vnf_availability_zones.index(myVMDict['availability_zone'])
                else:
                    av_index = None

                vm_id, _ = myvim.new_vminstance(myVMDict['name'], myVMDict['description'], myVMDict.get('start', None),
                                             myVMDict['imageRef'], myVMDict['flavorRef'], myVMDict['networks'],
                                             availability_zone_index=av_index,
                                             availability_zone_list=vnf_availability_zones)
                #print "VIM vm instance id (server id) for scenario %s: %s" % (scenarioDict['name'],vm_id)
                vm['vim_id'] = vm_id
                rollbackList.append({'what':'vm','where':'vim','vim_id':datacenter_id,'uuid':vm_id})
                #put interface uuid back to scenario[vnfs][vms[[interfaces]
                for net in myVMDict['networks']:
                    if "vim_id" in net:
                        for iface in vm['interfaces']:
                            if net["name"]==iface["internal_name"]:
                                iface["vim_id"]=net["vim_id"]
                                break

        logger.debug("start scenario Deployment done")
        #print yaml.safe_dump(scenarioDict, indent=4, default_flow_style=False)
        #r,c = mydb.new_instance_scenario_as_a_whole(nfvo_tenant,scenarioDict['name'],scenarioDict)
        instance_id = mydb.new_instance_scenario_as_a_whole(tenant_id,instance_scenario_name, instance_scenario_description, scenarioDict)
        return mydb.get_instance_scenario(instance_id)

    except (db_base_Exception, vimconn.vimconnException) as e:
        _, message = rollback(mydb, vims, rollbackList)
        if isinstance(e, db_base_Exception):
            error_text = "Exception at database"
        else:
            error_text = "Exception at VIM"
        error_text += " {} {}. {}".format(type(e).__name__, str(e), message)
        #logger.error("start_scenario %s", error_text)
        raise NfvoException(error_text, e.http_code)

def unify_cloud_config(cloud_config_preserve, cloud_config):
    """ join the cloud config information into cloud_config_preserve.
    In case of conflict cloud_config_preserve preserves
    None is allowed
    """
    if not cloud_config_preserve and not cloud_config:
        return None

    new_cloud_config = {"key-pairs":[], "users":[]}
    # key-pairs
    if cloud_config_preserve:
        for key in cloud_config_preserve.get("key-pairs", () ):
            if key not in new_cloud_config["key-pairs"]:
                new_cloud_config["key-pairs"].append(key)
    if cloud_config:
        for key in cloud_config.get("key-pairs", () ):
            if key not in new_cloud_config["key-pairs"]:
                new_cloud_config["key-pairs"].append(key)
    if not new_cloud_config["key-pairs"]:
        del new_cloud_config["key-pairs"]

    # users
    if cloud_config:
        new_cloud_config["users"] += cloud_config.get("users", () )
    if cloud_config_preserve:
        new_cloud_config["users"] += cloud_config_preserve.get("users", () )
    index_to_delete = []
    users = new_cloud_config.get("users", [])
    for index0 in range(0,len(users)):
        if index0 in index_to_delete:
            continue
        for index1 in range(index0+1,len(users)):
            if index1 in index_to_delete:
                continue
            if users[index0]["name"] == users[index1]["name"]:
                index_to_delete.append(index1)
                for key in users[index1].get("key-pairs",()):
                    if "key-pairs" not in users[index0]:
                        users[index0]["key-pairs"] = [key]
                    elif key not in users[index0]["key-pairs"]:
                        users[index0]["key-pairs"].append(key)
    index_to_delete.sort(reverse=True)
    for index in index_to_delete:
        del users[index]
    if not new_cloud_config["users"]:
        del new_cloud_config["users"]

    #boot-data-drive
    if cloud_config and cloud_config.get("boot-data-drive") != None:
        new_cloud_config["boot-data-drive"] = cloud_config["boot-data-drive"]
    if cloud_config_preserve and cloud_config_preserve.get("boot-data-drive") != None:
        new_cloud_config["boot-data-drive"] = cloud_config_preserve["boot-data-drive"]

    # user-data
    new_cloud_config["user-data"] = []
    if cloud_config and cloud_config.get("user-data"):
        if isinstance(cloud_config["user-data"], list):
            new_cloud_config["user-data"] += cloud_config["user-data"]
        else:
            new_cloud_config["user-data"].append(cloud_config["user-data"])
    if cloud_config_preserve and cloud_config_preserve.get("user-data"):
        if isinstance(cloud_config_preserve["user-data"], list):
            new_cloud_config["user-data"] += cloud_config_preserve["user-data"]
        else:
            new_cloud_config["user-data"].append(cloud_config_preserve["user-data"])
    if not new_cloud_config["user-data"]:
        del new_cloud_config["user-data"]

    # config files
    new_cloud_config["config-files"] = []
    if cloud_config and cloud_config.get("config-files") != None:
        new_cloud_config["config-files"] += cloud_config["config-files"]
    if cloud_config_preserve:
        for file in cloud_config_preserve.get("config-files", ()):
            for index in range(0, len(new_cloud_config["config-files"])):
                if new_cloud_config["config-files"][index]["dest"] == file["dest"]:
                    new_cloud_config["config-files"][index] = file
                    break
            else:
                new_cloud_config["config-files"].append(file)
    if not new_cloud_config["config-files"]:
        del new_cloud_config["config-files"]
    return new_cloud_config


def get_vim_thread(mydb, tenant_id, datacenter_id_name=None, datacenter_tenant_id=None):
    datacenter_id = None
    datacenter_name = None
    thread = None
    try:
        if datacenter_tenant_id:
            thread_id = datacenter_tenant_id
            thread = vim_threads["running"].get(datacenter_tenant_id)
        else:
            where_={"td.nfvo_tenant_id": tenant_id}
            if datacenter_id_name:
                if utils.check_valid_uuid(datacenter_id_name):
                    datacenter_id = datacenter_id_name
                    where_["dt.datacenter_id"] = datacenter_id
                else:
                    datacenter_name = datacenter_id_name
                    where_["d.name"] = datacenter_name
            if datacenter_tenant_id:
                where_["dt.uuid"] = datacenter_tenant_id
            datacenters = mydb.get_rows(
                SELECT=("dt.uuid as datacenter_tenant_id",),
                FROM="datacenter_tenants as dt join tenants_datacenters as td on dt.uuid=td.datacenter_tenant_id "
                     "join datacenters as d on d.uuid=dt.datacenter_id",
                WHERE=where_)
            if len(datacenters) > 1:
                raise NfvoException("More than one datacenters found, try to identify with uuid", httperrors.Conflict)
            elif datacenters:
                thread_id = datacenters[0]["datacenter_tenant_id"]
                thread = vim_threads["running"].get(thread_id)
        if not thread:
            raise NfvoException("datacenter '{}' not found".format(str(datacenter_id_name)), httperrors.Not_Found)
        return thread_id, thread
    except db_base_Exception as e:
        raise NfvoException("{} {}".format(type(e).__name__ , str(e)), e.http_code)


def get_datacenter_uuid(mydb, tenant_id, datacenter_id_name):
    WHERE_dict={}
    if utils.check_valid_uuid(datacenter_id_name):
        WHERE_dict['d.uuid'] = datacenter_id_name
    else:
        WHERE_dict['d.name'] = datacenter_id_name

    if tenant_id:
        WHERE_dict['nfvo_tenant_id'] = tenant_id
        from_= "tenants_datacenters as td join datacenters as d on td.datacenter_id=d.uuid join datacenter_tenants as" \
               " dt on td.datacenter_tenant_id=dt.uuid"
    else:
        from_ = 'datacenters as d'
    vimaccounts = mydb.get_rows(FROM=from_, SELECT=("d.uuid as uuid, d.name as name",), WHERE=WHERE_dict )
    if len(vimaccounts) == 0:
        raise NfvoException("datacenter '{}' not found".format(str(datacenter_id_name)), httperrors.Not_Found)
    elif len(vimaccounts)>1:
        #print "nfvo.datacenter_action() error. Several datacenters found"
        raise NfvoException("More than one datacenters found, try to identify with uuid", httperrors.Conflict)
    return vimaccounts[0]["uuid"], vimaccounts[0]["name"]


def get_datacenter_by_name_uuid(mydb, tenant_id, datacenter_id_name=None, **extra_filter):
    datacenter_id = None
    datacenter_name = None
    if datacenter_id_name:
        if utils.check_valid_uuid(datacenter_id_name):
            datacenter_id = datacenter_id_name
        else:
            datacenter_name = datacenter_id_name
    vims = get_vim(mydb, tenant_id, datacenter_id, datacenter_name, **extra_filter)
    if len(vims) == 0:
        raise NfvoException("datacenter '{}' not found".format(str(datacenter_id_name)), httperrors.Not_Found)
    elif len(vims)>1:
        #print "nfvo.datacenter_action() error. Several datacenters found"
        raise NfvoException("More than one datacenters found, try to identify with uuid", httperrors.Conflict)
    return vims.keys()[0], vims.values()[0]


def update(d, u):
    """Takes dict d and updates it with the values in dict u.
       It merges all depth levels"""
    for k, v in u.iteritems():
        if isinstance(v, collections.Mapping):
            r = update(d.get(k, {}), v)
            d[k] = r
        else:
            d[k] = u[k]
    return d


def create_instance(mydb, tenant_id, instance_dict):
    # print "Checking that nfvo_tenant_id exists and getting the VIM URI and the VIM tenant_id"
    # logger.debug("Creating instance...")
    scenario = instance_dict["scenario"]

    # find main datacenter
    myvims = {}
    myvim_threads_id = {}
    datacenter = instance_dict.get("datacenter")
    default_wim_account = instance_dict.get("wim_account")
    default_datacenter_id, vim = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)
    myvims[default_datacenter_id] = vim
    myvim_threads_id[default_datacenter_id], _ = get_vim_thread(mydb, tenant_id, default_datacenter_id)
    tenant = mydb.get_rows_by_id('nfvo_tenants', tenant_id)
    # myvim_tenant = myvim['tenant_id']
    rollbackList = []

    # print "Checking that the scenario exists and getting the scenario dictionary"
    if isinstance(scenario, str):
        scenarioDict = mydb.get_scenario(scenario, tenant_id, datacenter_vim_id=myvim_threads_id[default_datacenter_id],
                                         datacenter_id=default_datacenter_id)
    else:
        scenarioDict = scenario
        scenarioDict["uuid"] = None

    # logger.debug(">>>>>> Dictionaries before merging")
    # logger.debug(">>>>>> InstanceDict:\n{}".format(yaml.safe_dump(instance_dict,default_flow_style=False, width=256)))
    # logger.debug(">>>>>> ScenarioDict:\n{}".format(yaml.safe_dump(scenarioDict,default_flow_style=False, width=256)))

    db_instance_vnfs = []
    db_instance_vms = []
    db_instance_interfaces = []
    db_instance_sfis = []
    db_instance_sfs = []
    db_instance_classifications = []
    db_instance_sfps = []
    db_ip_profiles = []
    db_vim_actions = []
    uuid_list = []
    task_index = 0
    instance_name = instance_dict["name"]
    instance_uuid = str(uuid4())
    uuid_list.append(instance_uuid)
    db_instance_scenario = {
        "uuid": instance_uuid,
        "name": instance_name,
        "tenant_id": tenant_id,
        "scenario_id": scenarioDict['uuid'],
        "datacenter_id": default_datacenter_id,
        # filled bellow 'datacenter_tenant_id'
        "description": instance_dict.get("description"),
    }
    if scenarioDict.get("cloud-config"):
        db_instance_scenario["cloud_config"] = yaml.safe_dump(scenarioDict["cloud-config"],
                                                              default_flow_style=True, width=256)
    instance_action_id = get_task_id()
    db_instance_action = {
        "uuid": instance_action_id,   # same uuid for the instance and the action on create
        "tenant_id": tenant_id,
        "instance_id": instance_uuid,
        "description": "CREATE",
    }

    # Auxiliary dictionaries from x to y
    sce_net2instance = {}
    net2task_id = {'scenario': {}}
    # Mapping between local networks and WIMs
    wim_usage = {}

    def ip_profile_IM2RO(ip_profile_im):
        # translate from input format to database format
        ip_profile_ro = {}
        if 'subnet-address' in ip_profile_im:
            ip_profile_ro['subnet_address'] = ip_profile_im['subnet-address']
        if 'ip-version' in ip_profile_im:
            ip_profile_ro['ip_version'] = ip_profile_im['ip-version']
        if 'gateway-address' in ip_profile_im:
            ip_profile_ro['gateway_address'] = ip_profile_im['gateway-address']
        if 'dns-address' in ip_profile_im:
            ip_profile_ro['dns_address'] = ip_profile_im['dns-address']
            if isinstance(ip_profile_ro['dns_address'], (list, tuple)):
                ip_profile_ro['dns_address'] = ";".join(ip_profile_ro['dns_address'])
        if 'dhcp' in ip_profile_im:
            ip_profile_ro['dhcp_start_address'] = ip_profile_im['dhcp'].get('start-address')
            ip_profile_ro['dhcp_enabled'] = ip_profile_im['dhcp'].get('enabled', True)
            ip_profile_ro['dhcp_count'] = ip_profile_im['dhcp'].get('count')
        return ip_profile_ro

    # logger.debug("Creating instance from scenario-dict:\n%s",
    #               yaml.safe_dump(scenarioDict, indent=4, default_flow_style=False))
    try:
        # 0 check correct parameters
        for net_name, net_instance_desc in instance_dict.get("networks", {}).iteritems():
            for scenario_net in scenarioDict['nets']:
                if net_name == scenario_net.get("name") or net_name == scenario_net.get("osm_id") or net_name == scenario_net.get("uuid"):
                    break
            else:
                raise NfvoException("Invalid scenario network name or id '{}' at instance:networks".format(net_name),
                                    httperrors.Bad_Request)
            if "sites" not in net_instance_desc:
                net_instance_desc["sites"] = [ {} ]
            site_without_datacenter_field = False
            for site in net_instance_desc["sites"]:
                if site.get("datacenter"):
                    site["datacenter"], _ = get_datacenter_uuid(mydb, tenant_id, site["datacenter"])
                    if site["datacenter"] not in myvims:
                        # Add this datacenter to myvims
                        d, v = get_datacenter_by_name_uuid(mydb, tenant_id, site["datacenter"])
                        myvims[d] = v
                        myvim_threads_id[d], _ = get_vim_thread(mydb, tenant_id, site["datacenter"])
                        site["datacenter"] = d  # change name to id
                else:
                    if site_without_datacenter_field:
                        raise NfvoException("Found more than one entries without datacenter field at "
                                            "instance:networks:{}:sites".format(net_name), httperrors.Bad_Request)
                    site_without_datacenter_field = True
                    site["datacenter"] = default_datacenter_id   # change name to id

        for vnf_name, vnf_instance_desc in instance_dict.get("vnfs",{}).iteritems():
            for scenario_vnf in scenarioDict['vnfs']:
                if vnf_name == scenario_vnf['member_vnf_index'] or vnf_name == scenario_vnf['uuid'] or vnf_name == scenario_vnf['name']:
                    break
            else:
                raise NfvoException("Invalid vnf name '{}' at instance:vnfs".format(vnf_name), httperrors.Bad_Request)
            if "datacenter" in vnf_instance_desc:
                # Add this datacenter to myvims
                vnf_instance_desc["datacenter"], _ = get_datacenter_uuid(mydb, tenant_id, vnf_instance_desc["datacenter"])
                if vnf_instance_desc["datacenter"] not in myvims:
                    d, v = get_datacenter_by_name_uuid(mydb, tenant_id, vnf_instance_desc["datacenter"])
                    myvims[d] = v
                    myvim_threads_id[d], _ = get_vim_thread(mydb, tenant_id, vnf_instance_desc["datacenter"])
                scenario_vnf["datacenter"] = vnf_instance_desc["datacenter"]

            for net_id, net_instance_desc in vnf_instance_desc.get("networks", {}).iteritems():
                for scenario_net in scenario_vnf['nets']:
                    if net_id == scenario_net['osm_id'] or net_id == scenario_net['uuid'] or net_id == scenario_net["name"]:
                        break
                else:
                    raise NfvoException("Invalid net id or name '{}' at instance:vnfs:networks".format(net_id), httperrors.Bad_Request)
                if net_instance_desc.get("vim-network-name"):
                    scenario_net["vim-network-name"] = net_instance_desc["vim-network-name"]
                if net_instance_desc.get("vim-network-id"):
                    scenario_net["vim-network-id"] = net_instance_desc["vim-network-id"]
                if net_instance_desc.get("name"):
                    scenario_net["name"] = net_instance_desc["name"]
                if 'ip-profile' in net_instance_desc:
                    ipprofile_db = ip_profile_IM2RO(net_instance_desc['ip-profile'])
                    if 'ip_profile' not in scenario_net:
                        scenario_net['ip_profile'] = ipprofile_db
                    else:
                        update(scenario_net['ip_profile'], ipprofile_db)

            for vdu_id, vdu_instance_desc in vnf_instance_desc.get("vdus", {}).iteritems():
                for scenario_vm in scenario_vnf['vms']:
                    if vdu_id == scenario_vm['osm_id'] or vdu_id == scenario_vm["name"]:
                        break
                else:
                    raise NfvoException("Invalid vdu id or name '{}' at instance:vnfs:vdus".format(vdu_id), httperrors.Bad_Request)
                scenario_vm["instance_parameters"] = vdu_instance_desc
                for iface_id, iface_instance_desc in vdu_instance_desc.get("interfaces", {}).iteritems():
                    for scenario_interface in scenario_vm['interfaces']:
                        if iface_id == scenario_interface['internal_name'] or iface_id == scenario_interface["external_name"]:
                            scenario_interface.update(iface_instance_desc)
                            break
                    else:
                        raise NfvoException("Invalid vdu id or name '{}' at instance:vnfs:vdus".format(vdu_id), httperrors.Bad_Request)

        # 0.1 parse cloud-config parameters
        cloud_config = unify_cloud_config(instance_dict.get("cloud-config"), scenarioDict.get("cloud-config"))

        # 0.2 merge instance information into scenario
        # Ideally, the operation should be as simple as: update(scenarioDict,instance_dict)
        # However, this is not possible yet.
        for net_name, net_instance_desc in instance_dict.get("networks", {}).iteritems():
            for scenario_net in scenarioDict['nets']:
                if net_name == scenario_net.get("name") or net_name == scenario_net.get("osm_id") or net_name == scenario_net.get("uuid"):
                    if "wim_account" in net_instance_desc and net_instance_desc["wim_account"] is not None:
                        scenario_net["wim_account"] = net_instance_desc["wim_account"]
                    if 'ip-profile' in net_instance_desc:
                        ipprofile_db = ip_profile_IM2RO(net_instance_desc['ip-profile'])
                        if 'ip_profile' not in scenario_net:
                            scenario_net['ip_profile'] = ipprofile_db
                        else:
                            update(scenario_net['ip_profile'], ipprofile_db)
            for interface in net_instance_desc.get('interfaces', ()):
                if 'ip_address' in interface:
                    for vnf in scenarioDict['vnfs']:
                        if interface['vnf'] == vnf['name']:
                            for vnf_interface in vnf['interfaces']:
                                if interface['vnf_interface'] == vnf_interface['external_name']:
                                    vnf_interface['ip_address'] = interface['ip_address']

        # logger.debug(">>>>>>>> Merged dictionary")
        # logger.debug("Creating instance scenario-dict MERGED:\n%s",
        #              yaml.safe_dump(scenarioDict, indent=4, default_flow_style=False))

        # 1. Creating new nets (sce_nets) in the VIM"
        number_mgmt_networks = 0
        db_instance_nets = []
        for sce_net in scenarioDict['nets']:
            sce_net_uuid = sce_net.get('uuid', sce_net["name"])
            # get involved datacenters where this network need to be created
            involved_datacenters = []
            for sce_vnf in scenarioDict.get("vnfs", ()):
                vnf_datacenter = sce_vnf.get("datacenter", default_datacenter_id)
                if vnf_datacenter in involved_datacenters:
                    continue
                if sce_vnf.get("interfaces"):
                    for sce_vnf_ifaces in sce_vnf["interfaces"]:
                        if sce_vnf_ifaces.get("sce_net_id") == sce_net["uuid"]:
                            involved_datacenters.append(vnf_datacenter)
                            break
            if not involved_datacenters:
                involved_datacenters.append(default_datacenter_id)
            target_wim_account = sce_net.get("wim_account", default_wim_account)

            # --> WIM
            # TODO: use this information during network creation
            wim_account_id = wim_account_name = None
            if len(involved_datacenters) > 1 and 'uuid' in sce_net:
                if target_wim_account is None or target_wim_account is True:  # automatic selection of WIM
                    # OBS: sce_net without uuid are used internally to VNFs
                    # and the assumption is that VNFs will not be split among
                    # different datacenters
                    wim_account = wim_engine.find_suitable_wim_account(
                        involved_datacenters, tenant_id)
                    wim_account_id = wim_account['uuid']
                    wim_account_name = wim_account['name']
                    wim_usage[sce_net['uuid']] = wim_account_id
                elif isinstance(target_wim_account, str):     # manual selection of WIM
                    wim_account.persist.get_wim_account_by(target_wim_account, tenant_id)
                    wim_account_id = wim_account['uuid']
                    wim_account_name = wim_account['name']
                    wim_usage[sce_net['uuid']] = wim_account_id
                else:  # not WIM usage
                    wim_usage[sce_net['uuid']] = False
            # <-- WIM

            descriptor_net = {}
            if instance_dict.get("networks"):
                if sce_net.get("uuid") in instance_dict["networks"]:
                    descriptor_net = instance_dict["networks"][sce_net["uuid"]]
                    descriptor_net_name = sce_net["uuid"]
                elif sce_net.get("osm_id") in instance_dict["networks"]:
                    descriptor_net = instance_dict["networks"][sce_net["osm_id"]]
                    descriptor_net_name = sce_net["osm_id"]
                elif sce_net["name"] in instance_dict["networks"]:
                    descriptor_net = instance_dict["networks"][sce_net["name"]]
                    descriptor_net_name = sce_net["name"]
            net_name = descriptor_net.get("vim-network-name")
            # add datacenters from instantiation parameters
            if descriptor_net.get("sites"):
                for site in descriptor_net["sites"]:
                    if site.get("datacenter") and site["datacenter"] not in involved_datacenters:
                        involved_datacenters.append(site["datacenter"])
            sce_net2instance[sce_net_uuid] = {}
            net2task_id['scenario'][sce_net_uuid] = {}

            use_network = None
            related_network = None
            if descriptor_net.get("use-network"):
                target_instance_nets = mydb.get_rows(
                    SELECT="related",
                    FROM="instance_nets",
                    WHERE={"instance_scenario_id": descriptor_net["use-network"]["instance_scenario_id"],
                           "osm_id":  descriptor_net["use-network"]["osm_id"]},
                )
                if not target_instance_nets:
                    raise NfvoException(
                        "Cannot find the target network at instance:networks[{}]:use-network".format(descriptor_net_name),
                        httperrors.Bad_Request)
                else:
                    use_network = target_instance_nets[0]["related"]

            if sce_net["external"]:
                number_mgmt_networks += 1

            for datacenter_id in involved_datacenters:
                netmap_use = None
                netmap_create = None
                if descriptor_net.get("sites"):
                    for site in descriptor_net["sites"]:
                        if site.get("datacenter") == datacenter_id:
                            netmap_use = site.get("netmap-use")
                            netmap_create = site.get("netmap-create")
                            break

                vim = myvims[datacenter_id]
                myvim_thread_id = myvim_threads_id[datacenter_id]

                net_type = sce_net['type']
                net_vim_name = None
                lookfor_filter = {'admin_state_up': True, 'status': 'ACTIVE'}  # 'shared': True

                if not net_name:
                    if sce_net["external"]:
                        net_name = sce_net["name"]
                    else:
                        net_name = "{}-{}".format(instance_name, sce_net["name"])
                        net_name = net_name[:255]     # limit length

                if netmap_use or netmap_create:
                    create_network = False
                    lookfor_network = False
                    if netmap_use:
                        lookfor_network = True
                        if utils.check_valid_uuid(netmap_use):
                            lookfor_filter["id"] = netmap_use
                        else:
                            lookfor_filter["name"] = netmap_use
                    if netmap_create:
                        create_network = True
                        net_vim_name = net_name
                        if isinstance(netmap_create, str):
                            net_vim_name = netmap_create
                elif sce_net.get("vim_network_name"):
                    create_network = False
                    lookfor_network = True
                    lookfor_filter["name"] = sce_net.get("vim_network_name")
                elif sce_net["external"]:
                    if sce_net.get('vim_id'):
                        # there is a netmap at datacenter_nets database   # TODO REVISE!!!!
                        create_network = False
                        lookfor_network = True
                        lookfor_filter["id"] = sce_net['vim_id']
                    elif vim["config"].get("management_network_id") or vim["config"].get("management_network_name"):
                        if number_mgmt_networks > 1:
                            raise NfvoException("Found several VLD of type mgmt. "
                                                "You must concrete what vim-network must be use for each one",
                                                httperrors.Bad_Request)
                        create_network = False
                        lookfor_network = True
                        if vim["config"].get("management_network_id"):
                            lookfor_filter["id"] = vim["config"]["management_network_id"]
                        else:
                            lookfor_filter["name"] = vim["config"]["management_network_name"]
                    else:
                        # There is not a netmap, look at datacenter for a net with this name and create if not found
                        create_network = True
                        lookfor_network = True
                        lookfor_filter["name"] = sce_net["name"]
                        net_vim_name = sce_net["name"]
                else:
                    net_vim_name = net_name
                    create_network = True
                    lookfor_network = False

                task_extra = {}
                if create_network:
                    task_action = "CREATE"
                    task_extra["params"] = (net_vim_name, net_type, sce_net.get('ip_profile', None), wim_account_name)
                    if lookfor_network:
                        task_extra["find"] = (lookfor_filter,)
                elif lookfor_network:
                    task_action = "FIND"
                    task_extra["params"] = (lookfor_filter,)

                # fill database content
                net_uuid = str(uuid4())
                uuid_list.append(net_uuid)
                sce_net2instance[sce_net_uuid][datacenter_id] = net_uuid
                if not related_network:   # all db_instance_nets will have same related
                    related_network = use_network or net_uuid
                db_net = {
                    "uuid": net_uuid,
                    "osm_id": sce_net.get("osm_id") or sce_net["name"],
                    "related": related_network,
                    'vim_net_id': None,
                    "vim_name": net_vim_name,
                    "instance_scenario_id": instance_uuid,
                    "sce_net_id": sce_net.get("uuid"),
                    "created": create_network,
                    'datacenter_id': datacenter_id,
                    'datacenter_tenant_id': myvim_thread_id,
                    'status': 'BUILD' #  if create_network else "ACTIVE"
                }
                db_instance_nets.append(db_net)
                db_vim_action = {
                    "instance_action_id": instance_action_id,
                    "status": "SCHEDULED",
                    "task_index": task_index,
                    "datacenter_vim_id": myvim_thread_id,
                    "action": task_action,
                    "item": "instance_nets",
                    "item_id": net_uuid,
                    "related": related_network,
                    "extra": yaml.safe_dump(task_extra, default_flow_style=True, width=256)
                }
                net2task_id['scenario'][sce_net_uuid][datacenter_id] = task_index
                task_index += 1
                db_vim_actions.append(db_vim_action)

            if 'ip_profile' in sce_net:
                db_ip_profile={
                    'instance_net_id': net_uuid,
                    'ip_version': sce_net['ip_profile']['ip_version'],
                    'subnet_address': sce_net['ip_profile']['subnet_address'],
                    'gateway_address': sce_net['ip_profile']['gateway_address'],
                    'dns_address': sce_net['ip_profile']['dns_address'],
                    'dhcp_enabled': sce_net['ip_profile']['dhcp_enabled'],
                    'dhcp_start_address': sce_net['ip_profile']['dhcp_start_address'],
                    'dhcp_count': sce_net['ip_profile']['dhcp_count'],
                }
                db_ip_profiles.append(db_ip_profile)

        # Create VNFs
        vnf_params = {
            "default_datacenter_id": default_datacenter_id,
            "myvim_threads_id": myvim_threads_id,
            "instance_uuid": instance_uuid,
            "instance_name": instance_name,
            "instance_action_id": instance_action_id,
            "myvims": myvims,
            "cloud_config": cloud_config,
            "RO_pub_key": tenant[0].get('RO_pub_key'),
            "instance_parameters": instance_dict,
        }
        vnf_params_out = {
            "task_index": task_index,
            "uuid_list": uuid_list,
            "db_instance_nets": db_instance_nets,
            "db_vim_actions": db_vim_actions,
            "db_ip_profiles": db_ip_profiles,
            "db_instance_vnfs": db_instance_vnfs,
            "db_instance_vms": db_instance_vms,
            "db_instance_interfaces": db_instance_interfaces,
            "net2task_id": net2task_id,
            "sce_net2instance": sce_net2instance,
        }
        # sce_vnf_list = sorted(scenarioDict['vnfs'], key=lambda k: k['name'])
        for sce_vnf in scenarioDict.get('vnfs', ()):  # sce_vnf_list:
            instantiate_vnf(mydb, sce_vnf, vnf_params, vnf_params_out, rollbackList)
        task_index = vnf_params_out["task_index"]
        uuid_list = vnf_params_out["uuid_list"]

        # Create VNFFGs
        # task_depends_on = []
        for vnffg in scenarioDict.get('vnffgs', ()):
            for rsp in vnffg['rsps']:
                sfs_created = []
                for cp in rsp['connection_points']:
                    count = mydb.get_rows(
                            SELECT='vms.count',
                            FROM="vms join interfaces on vms.uuid=interfaces.vm_id join sce_rsp_hops as h "
                                 "on interfaces.uuid=h.ingress_interface_id",
                            WHERE={'h.uuid': cp['uuid']})[0]['count']
                    instance_vnf = next((item for item in db_instance_vnfs if item['sce_vnf_id'] == cp['sce_vnf_id']), None)
                    instance_vms = [item for item in db_instance_vms if item['instance_vnf_id'] == instance_vnf['uuid']]
                    dependencies = []
                    for instance_vm in instance_vms:
                        action = next((item for item in db_vim_actions if item['item_id'] == instance_vm['uuid']), None)
                        if action:
                            dependencies.append(action['task_index'])
                        # TODO: throw exception if count != len(instance_vms)
                        # TODO: and action shouldn't ever be None
                    sfis_created = []
                    for i in range(count):
                        # create sfis
                        sfi_uuid = str(uuid4())
                        extra_params = {
                            "ingress_interface_id": cp["ingress_interface_id"],
                            "egress_interface_id": cp["egress_interface_id"]
                        }
                        uuid_list.append(sfi_uuid)
                        db_sfi = {
                            "uuid": sfi_uuid,
                            "related": sfi_uuid,
                            "instance_scenario_id": instance_uuid,
                            'sce_rsp_hop_id': cp['uuid'],
                            'datacenter_id': datacenter_id,
                            'datacenter_tenant_id': myvim_thread_id,
                            "vim_sfi_id": None, # vim thread will populate
                        }
                        db_instance_sfis.append(db_sfi)
                        db_vim_action = {
                            "instance_action_id": instance_action_id,
                            "task_index": task_index,
                            "datacenter_vim_id": myvim_thread_id,
                            "action": "CREATE",
                            "status": "SCHEDULED",
                            "item": "instance_sfis",
                            "item_id": sfi_uuid,
                            "related": sfi_uuid,
                            "extra": yaml.safe_dump({"params": extra_params, "depends_on": [dependencies[i]]},
                                                    default_flow_style=True, width=256)
                        }
                        sfis_created.append(task_index)
                        task_index += 1
                        db_vim_actions.append(db_vim_action)
                    # create sfs
                    sf_uuid = str(uuid4())
                    uuid_list.append(sf_uuid)
                    db_sf = {
                        "uuid": sf_uuid,
                        "related": sf_uuid,
                        "instance_scenario_id": instance_uuid,
                        'sce_rsp_hop_id': cp['uuid'],
                        'datacenter_id': datacenter_id,
                        'datacenter_tenant_id': myvim_thread_id,
                        "vim_sf_id": None, # vim thread will populate
                    }
                    db_instance_sfs.append(db_sf)
                    db_vim_action = {
                        "instance_action_id": instance_action_id,
                        "task_index": task_index,
                        "datacenter_vim_id": myvim_thread_id,
                        "action": "CREATE",
                        "status": "SCHEDULED",
                        "item": "instance_sfs",
                        "item_id": sf_uuid,
                        "related": sf_uuid,
                        "extra": yaml.safe_dump({"params": "", "depends_on": sfis_created},
                                                default_flow_style=True, width=256)
                    }
                    sfs_created.append(task_index)
                    task_index += 1
                    db_vim_actions.append(db_vim_action)
                classifier = rsp['classifier']

                # TODO the following ~13 lines can be reused for the sfi case
                count = mydb.get_rows(
                        SELECT=('vms.count'),
                        FROM="vms join interfaces on vms.uuid=interfaces.vm_id join sce_classifiers as c on interfaces.uuid=c.interface_id",
                        WHERE={'c.uuid': classifier['uuid']})[0]['count']
                instance_vnf = next((item for item in db_instance_vnfs if item['sce_vnf_id'] == classifier['sce_vnf_id']), None)
                instance_vms = [item for item in db_instance_vms if item['instance_vnf_id'] == instance_vnf['uuid']]
                dependencies = []
                for instance_vm in instance_vms:
                    action = next((item for item in db_vim_actions if item['item_id'] == instance_vm['uuid']), None)
                    if action:
                        dependencies.append(action['task_index'])
                    # TODO: throw exception if count != len(instance_vms)
                    # TODO: and action shouldn't ever be None
                classifications_created = []
                for i in range(count):
                    for match in classifier['matches']:
                        # create classifications
                        classification_uuid = str(uuid4())
                        uuid_list.append(classification_uuid)
                        db_classification = {
                            "uuid": classification_uuid,
                            "related": classification_uuid,
                            "instance_scenario_id": instance_uuid,
                            'sce_classifier_match_id': match['uuid'],
                            'datacenter_id': datacenter_id,
                            'datacenter_tenant_id': myvim_thread_id,
                            "vim_classification_id": None, # vim thread will populate
                        }
                        db_instance_classifications.append(db_classification)
                        classification_params = {
                            "ip_proto": match["ip_proto"],
                            "source_ip": match["source_ip"],
                            "destination_ip": match["destination_ip"],
                            "source_port": match["source_port"],
                            "destination_port": match["destination_port"]
                        }
                        db_vim_action = {
                            "instance_action_id": instance_action_id,
                            "task_index": task_index,
                            "datacenter_vim_id": myvim_thread_id,
                            "action": "CREATE",
                            "status": "SCHEDULED",
                            "item": "instance_classifications",
                            "item_id": classification_uuid,
                            "related": classification_uuid,
                            "extra": yaml.safe_dump({"params": classification_params, "depends_on": [dependencies[i]]},
                                                    default_flow_style=True, width=256)
                        }
                        classifications_created.append(task_index)
                        task_index += 1
                        db_vim_actions.append(db_vim_action)

                # create sfps
                sfp_uuid = str(uuid4())
                uuid_list.append(sfp_uuid)
                db_sfp = {
                    "uuid": sfp_uuid,
                    "related": sfp_uuid,
                    "instance_scenario_id": instance_uuid,
                    'sce_rsp_id': rsp['uuid'],
                    'datacenter_id': datacenter_id,
                    'datacenter_tenant_id': myvim_thread_id,
                    "vim_sfp_id": None, # vim thread will populate
                }
                db_instance_sfps.append(db_sfp)
                db_vim_action = {
                    "instance_action_id": instance_action_id,
                    "task_index": task_index,
                    "datacenter_vim_id": myvim_thread_id,
                    "action": "CREATE",
                    "status": "SCHEDULED",
                    "item": "instance_sfps",
                    "item_id": sfp_uuid,
                    "related": sfp_uuid,
                    "extra": yaml.safe_dump({"params": "", "depends_on": sfs_created + classifications_created},
                                            default_flow_style=True, width=256)
                }
                task_index += 1
                db_vim_actions.append(db_vim_action)
        db_instance_action["number_tasks"] = task_index

        # --> WIM
        logger.debug('wim_usage:\n%s\n\n', pformat(wim_usage))
        wan_links = wim_engine.derive_wan_links(wim_usage, db_instance_nets, tenant_id)
        wim_actions = wim_engine.create_actions(wan_links)
        wim_actions, db_instance_action = (
            wim_engine.incorporate_actions(wim_actions, db_instance_action))
        # <-- WIM

        scenarioDict["datacenter2tenant"] = myvim_threads_id

        db_instance_scenario['datacenter_tenant_id'] = myvim_threads_id[default_datacenter_id]
        db_instance_scenario['datacenter_id'] = default_datacenter_id
        db_tables=[
            {"instance_scenarios": db_instance_scenario},
            {"instance_vnfs": db_instance_vnfs},
            {"instance_nets": db_instance_nets},
            {"ip_profiles": db_ip_profiles},
            {"instance_vms": db_instance_vms},
            {"instance_interfaces": db_instance_interfaces},
            {"instance_actions": db_instance_action},
            {"instance_sfis": db_instance_sfis},
            {"instance_sfs": db_instance_sfs},
            {"instance_classifications": db_instance_classifications},
            {"instance_sfps": db_instance_sfps},
            {"instance_wim_nets": wan_links},
            {"vim_wim_actions": db_vim_actions + wim_actions}
        ]

        logger.debug("create_instance done DB tables: %s",
                    yaml.safe_dump(db_tables, indent=4, default_flow_style=False) )
        mydb.new_rows(db_tables, uuid_list)
        for myvim_thread_id in myvim_threads_id.values():
            vim_threads["running"][myvim_thread_id].insert_task(db_vim_actions)

        wim_engine.dispatch(wim_actions)

        returned_instance = mydb.get_instance_scenario(instance_uuid)
        returned_instance["action_id"] = instance_action_id
        return returned_instance
    except (NfvoException, vimconn.vimconnException, wimconn.WimConnectorError, db_base_Exception) as e:
        message = rollback(mydb, myvims, rollbackList)
        if isinstance(e, db_base_Exception):
            error_text = "database Exception"
        elif isinstance(e, vimconn.vimconnException):
            error_text = "VIM Exception"
        elif isinstance(e, wimconn.WimConnectorError):
            error_text = "WIM Exception"
        else:
            error_text = "Exception"
        error_text += " {} {}. {}".format(type(e).__name__, str(e), message)
        # logger.error("create_instance: %s", error_text)
        logger.exception(e)
        raise NfvoException(error_text, e.http_code)


def instantiate_vnf(mydb, sce_vnf, params, params_out, rollbackList):
    default_datacenter_id = params["default_datacenter_id"]
    myvim_threads_id = params["myvim_threads_id"]
    instance_uuid = params["instance_uuid"]
    instance_name = params["instance_name"]
    instance_action_id = params["instance_action_id"]
    myvims = params["myvims"]
    cloud_config = params["cloud_config"]
    RO_pub_key = params["RO_pub_key"]

    task_index = params_out["task_index"]
    uuid_list = params_out["uuid_list"]
    db_instance_nets = params_out["db_instance_nets"]
    db_vim_actions = params_out["db_vim_actions"]
    db_ip_profiles = params_out["db_ip_profiles"]
    db_instance_vnfs = params_out["db_instance_vnfs"]
    db_instance_vms = params_out["db_instance_vms"]
    db_instance_interfaces = params_out["db_instance_interfaces"]
    net2task_id = params_out["net2task_id"]
    sce_net2instance = params_out["sce_net2instance"]

    vnf_net2instance = {}

    # 2. Creating new nets (vnf internal nets) in the VIM"
    # For each vnf net, we create it and we add it to instanceNetlist.
    if sce_vnf.get("datacenter"):
        datacenter_id = sce_vnf["datacenter"]
        myvim_thread_id = myvim_threads_id[sce_vnf["datacenter"]]
    else:
        datacenter_id = default_datacenter_id
        myvim_thread_id = myvim_threads_id[default_datacenter_id]
    for net in sce_vnf['nets']:
        # TODO revis
        # descriptor_net = instance_dict.get("vnfs", {}).get(sce_vnf["name"], {})
        # net_name = descriptor_net.get("name")
        net_name = None
        if not net_name:
            net_name = "{}-{}".format(instance_name, net["name"])
            net_name = net_name[:255]  # limit length
        net_type = net['type']

        if sce_vnf['uuid'] not in vnf_net2instance:
            vnf_net2instance[sce_vnf['uuid']] = {}
        if sce_vnf['uuid'] not in net2task_id:
            net2task_id[sce_vnf['uuid']] = {}
        net2task_id[sce_vnf['uuid']][net['uuid']] = task_index

        # fill database content
        net_uuid = str(uuid4())
        uuid_list.append(net_uuid)
        vnf_net2instance[sce_vnf['uuid']][net['uuid']] = net_uuid
        db_net = {
            "uuid": net_uuid,
            "related": net_uuid,
            'vim_net_id': None,
            "vim_name": net_name,
            "instance_scenario_id": instance_uuid,
            "net_id": net["uuid"],
            "created": True,
            'datacenter_id': datacenter_id,
            'datacenter_tenant_id': myvim_thread_id,
        }
        db_instance_nets.append(db_net)

        lookfor_filter = {}
        if net.get("vim-network-name"):
            lookfor_filter["name"] = net["vim-network-name"]
        if net.get("vim-network-id"):
            lookfor_filter["id"] = net["vim-network-id"]
        if lookfor_filter:
            task_action = "FIND"
            task_extra = {"params": (lookfor_filter,)}
        else:
            task_action = "CREATE"
            task_extra = {"params": (net_name, net_type, net.get('ip_profile', None))}

        db_vim_action = {
            "instance_action_id": instance_action_id,
            "task_index": task_index,
            "datacenter_vim_id": myvim_thread_id,
            "status": "SCHEDULED",
            "action": task_action,
            "item": "instance_nets",
            "item_id": net_uuid,
            "related": net_uuid,
            "extra": yaml.safe_dump(task_extra, default_flow_style=True, width=256)
        }
        task_index += 1
        db_vim_actions.append(db_vim_action)

        if 'ip_profile' in net:
            db_ip_profile = {
                'instance_net_id': net_uuid,
                'ip_version': net['ip_profile']['ip_version'],
                'subnet_address': net['ip_profile']['subnet_address'],
                'gateway_address': net['ip_profile']['gateway_address'],
                'dns_address': net['ip_profile']['dns_address'],
                'dhcp_enabled': net['ip_profile']['dhcp_enabled'],
                'dhcp_start_address': net['ip_profile']['dhcp_start_address'],
                'dhcp_count': net['ip_profile']['dhcp_count'],
            }
            db_ip_profiles.append(db_ip_profile)

    # print "vnf_net2instance:"
    # print yaml.safe_dump(vnf_net2instance, indent=4, default_flow_style=False)

    # 3. Creating new vm instances in the VIM
    # myvim.new_vminstance(self,vimURI,tenant_id,name,description,image_id,flavor_id,net_dict)
    ssh_access = None
    if sce_vnf.get('mgmt_access'):
        ssh_access = sce_vnf['mgmt_access'].get('config-access', {}).get('ssh-access')
    vnf_availability_zones = []
    for vm in sce_vnf.get('vms'):
        vm_av = vm.get('availability_zone')
        if vm_av and vm_av not in vnf_availability_zones:
            vnf_availability_zones.append(vm_av)

    # check if there is enough availability zones available at vim level.
    if myvims[datacenter_id].availability_zone and vnf_availability_zones:
        if len(vnf_availability_zones) > len(myvims[datacenter_id].availability_zone):
            raise NfvoException('No enough availability zones at VIM for this deployment', httperrors.Bad_Request)

    if sce_vnf.get("datacenter"):
        vim = myvims[sce_vnf["datacenter"]]
        myvim_thread_id = myvim_threads_id[sce_vnf["datacenter"]]
        datacenter_id = sce_vnf["datacenter"]
    else:
        vim = myvims[default_datacenter_id]
        myvim_thread_id = myvim_threads_id[default_datacenter_id]
        datacenter_id = default_datacenter_id
    sce_vnf["datacenter_id"] = datacenter_id
    i = 0

    vnf_uuid = str(uuid4())
    uuid_list.append(vnf_uuid)
    db_instance_vnf = {
        'uuid': vnf_uuid,
        'instance_scenario_id': instance_uuid,
        'vnf_id': sce_vnf['vnf_id'],
        'sce_vnf_id': sce_vnf['uuid'],
        'datacenter_id': datacenter_id,
        'datacenter_tenant_id': myvim_thread_id,
    }
    db_instance_vnfs.append(db_instance_vnf)

    for vm in sce_vnf['vms']:
        # skip PDUs
        if vm.get("pdu_type"):
            continue

        myVMDict = {}
        sce_vnf_name = sce_vnf['member_vnf_index'] if sce_vnf['member_vnf_index'] else sce_vnf['name']
        myVMDict['name'] = "{}-{}-{}".format(instance_name[:64], sce_vnf_name[:64], vm["name"][:64])
        myVMDict['description'] = myVMDict['name'][0:99]
        #                if not startvms:
        #                    myVMDict['start'] = "no"
        if vm.get("instance_parameters") and vm["instance_parameters"].get("name"):
            myVMDict['name'] = vm["instance_parameters"].get("name")
        myVMDict['name'] = myVMDict['name'][0:255]  # limit name length
        # create image at vim in case it not exist
        image_uuid = vm['image_id']
        if vm.get("image_list"):
            for alternative_image in vm["image_list"]:
                if alternative_image["vim_type"] == vim["config"]["_vim_type_internal"]:
                    image_uuid = alternative_image['image_id']
                    break
        image_dict = mydb.get_table_by_uuid_name("images", image_uuid)
        image_id = create_or_use_image(mydb, {datacenter_id: vim}, image_dict, [], True)
        vm['vim_image_id'] = image_id

        # create flavor at vim in case it not exist
        flavor_dict = mydb.get_table_by_uuid_name("flavors", vm['flavor_id'])
        if flavor_dict['extended'] != None:
            flavor_dict['extended'] = yaml.load(flavor_dict['extended'])
        flavor_id = create_or_use_flavor(mydb, {datacenter_id: vim}, flavor_dict, rollbackList, True)

        # Obtain information for additional disks
        extended_flavor_dict = mydb.get_rows(FROM='datacenters_flavors', SELECT=('extended',),
                                             WHERE={'vim_id': flavor_id})
        if not extended_flavor_dict:
            raise NfvoException("flavor '{}' not found".format(flavor_id), httperrors.Not_Found)

        # extended_flavor_dict_yaml = yaml.load(extended_flavor_dict[0])
        myVMDict['disks'] = None
        extended_info = extended_flavor_dict[0]['extended']
        if extended_info != None:
            extended_flavor_dict_yaml = yaml.load(extended_info)
            if 'disks' in extended_flavor_dict_yaml:
                myVMDict['disks'] = extended_flavor_dict_yaml['disks']
                if vm.get("instance_parameters") and vm["instance_parameters"].get("devices"):
                    for disk in myVMDict['disks']:
                        if disk.get("name") in vm["instance_parameters"]["devices"]:
                            disk.update(vm["instance_parameters"]["devices"][disk.get("name")])

        vm['vim_flavor_id'] = flavor_id
        myVMDict['imageRef'] = vm['vim_image_id']
        myVMDict['flavorRef'] = vm['vim_flavor_id']
        myVMDict['availability_zone'] = vm.get('availability_zone')
        myVMDict['networks'] = []
        task_depends_on = []
        # TODO ALF. connect_mgmt_interfaces. Connect management interfaces if this is true
        is_management_vm = False
        db_vm_ifaces = []
        for iface in vm['interfaces']:
            netDict = {}
            if iface['type'] == "data":
                netDict['type'] = iface['model']
            elif "model" in iface and iface["model"] != None:
                netDict['model'] = iface['model']
            # TODO in future, remove this because mac_address will not be set, and the type of PV,VF
            # is obtained from iterface table model
            # discover type of interface looking at flavor
            for numa in flavor_dict.get('extended', {}).get('numas', []):
                for flavor_iface in numa.get('interfaces', []):
                    if flavor_iface.get('name') == iface['internal_name']:
                        if flavor_iface['dedicated'] == 'yes':
                            netDict['type'] = "PF"  # passthrough
                        elif flavor_iface['dedicated'] == 'no':
                            netDict['type'] = "VF"  # siov
                        elif flavor_iface['dedicated'] == 'yes:sriov':
                            netDict['type'] = "VFnotShared"  # sriov but only one sriov on the PF
                        netDict["mac_address"] = flavor_iface.get("mac_address")
                        break
            netDict["use"] = iface['type']
            if netDict["use"] == "data" and not netDict.get("type"):
                # print "netDict", netDict
                # print "iface", iface
                e_text = "Cannot determine the interface type PF or VF of VNF '{}' VM '{}' iface '{}'".fromat(
                    sce_vnf['name'], vm['name'], iface['internal_name'])
                if flavor_dict.get('extended') == None:
                    raise NfvoException(e_text + "After database migration some information is not available. \
                            Try to delete and create the scenarios and VNFs again", httperrors.Conflict)
                else:
                    raise NfvoException(e_text, httperrors.Internal_Server_Error)
            if netDict["use"] == "mgmt":
                is_management_vm = True
                netDict["type"] = "virtual"
            if netDict["use"] == "bridge":
                netDict["type"] = "virtual"
            if iface.get("vpci"):
                netDict['vpci'] = iface['vpci']
            if iface.get("mac"):
                netDict['mac_address'] = iface['mac']
            if iface.get("mac_address"):
                netDict['mac_address'] = iface['mac_address']
            if iface.get("ip_address"):
                netDict['ip_address'] = iface['ip_address']
            if iface.get("port-security") is not None:
                netDict['port_security'] = iface['port-security']
            if iface.get("floating-ip") is not None:
                netDict['floating_ip'] = iface['floating-ip']
            netDict['name'] = iface['internal_name']
            if iface['net_id'] is None:
                for vnf_iface in sce_vnf["interfaces"]:
                    # print iface
                    # print vnf_iface
                    if vnf_iface['interface_id'] == iface['uuid']:
                        netDict['net_id'] = "TASK-{}".format(
                            net2task_id['scenario'][vnf_iface['sce_net_id']][datacenter_id])
                        instance_net_id = sce_net2instance[vnf_iface['sce_net_id']][datacenter_id]
                        task_depends_on.append(net2task_id['scenario'][vnf_iface['sce_net_id']][datacenter_id])
                        break
            else:
                netDict['net_id'] = "TASK-{}".format(net2task_id[sce_vnf['uuid']][iface['net_id']])
                instance_net_id = vnf_net2instance[sce_vnf['uuid']][iface['net_id']]
                task_depends_on.append(net2task_id[sce_vnf['uuid']][iface['net_id']])
            # skip bridge ifaces not connected to any net
            if 'net_id' not in netDict or netDict['net_id'] == None:
                continue
            myVMDict['networks'].append(netDict)
            db_vm_iface = {
                # "uuid"
                # 'instance_vm_id': instance_vm_uuid,
                "instance_net_id": instance_net_id,
                'interface_id': iface['uuid'],
                # 'vim_interface_id': ,
                'type': 'external' if iface['external_name'] is not None else 'internal',
                'ip_address': iface.get('ip_address'),
                'mac_address': iface.get('mac'),
                'floating_ip': int(iface.get('floating-ip', False)),
                'port_security': int(iface.get('port-security', True))
            }
            db_vm_ifaces.append(db_vm_iface)
        # print ">>>>>>>>>>>>>>>>>>>>>>>>>>>"
        # print myVMDict['name']
        # print "networks", yaml.safe_dump(myVMDict['networks'], indent=4, default_flow_style=False)
        # print "interfaces", yaml.safe_dump(vm['interfaces'], indent=4, default_flow_style=False)
        # print ">>>>>>>>>>>>>>>>>>>>>>>>>>>"

        # We add the RO key to cloud_config if vnf will need ssh access
        cloud_config_vm = cloud_config
        if is_management_vm and params["instance_parameters"].get("mgmt_keys"):
            cloud_config_vm = unify_cloud_config({"key-pairs": params["instance_parameters"]["mgmt_keys"]},
                                                  cloud_config_vm)

        if vm.get("instance_parameters") and vm["instance_parameters"].get("mgmt_keys"):
            cloud_config_vm = unify_cloud_config({"key-pairs": vm["instance_parameters"]["mgmt_keys"]},
                                                 cloud_config_vm)
        # if ssh_access and ssh_access['required'] and ssh_access['default-user'] and tenant[0].get('RO_pub_key'):
        #     RO_key = {"key-pairs": [tenant[0]['RO_pub_key']]}
        #     cloud_config_vm = unify_cloud_config(cloud_config_vm, RO_key)
        if vm.get("boot_data"):
            cloud_config_vm = unify_cloud_config(vm["boot_data"], cloud_config_vm)

        if myVMDict.get('availability_zone'):
            av_index = vnf_availability_zones.index(myVMDict['availability_zone'])
        else:
            av_index = None
        for vm_index in range(0, vm.get('count', 1)):
            vm_name = myVMDict['name'] + "-" + str(vm_index+1)
            task_params = (vm_name, myVMDict['description'], myVMDict.get('start', None),
                           myVMDict['imageRef'], myVMDict['flavorRef'], myVMDict['networks'], cloud_config_vm,
                           myVMDict['disks'], av_index, vnf_availability_zones)
            # put interface uuid back to scenario[vnfs][vms[[interfaces]
            for net in myVMDict['networks']:
                if "vim_id" in net:
                    for iface in vm['interfaces']:
                        if net["name"] == iface["internal_name"]:
                            iface["vim_id"] = net["vim_id"]
                            break
            vm_uuid = str(uuid4())
            uuid_list.append(vm_uuid)
            db_vm = {
                "uuid": vm_uuid,
                "related": vm_uuid,
                'instance_vnf_id': vnf_uuid,
                # TODO delete "vim_vm_id": vm_id,
                "vm_id": vm["uuid"],
                "vim_name": vm_name,
                # "status":
            }
            db_instance_vms.append(db_vm)

            iface_index = 0
            for db_vm_iface in db_vm_ifaces:
                iface_uuid = str(uuid4())
                uuid_list.append(iface_uuid)
                db_vm_iface_instance = {
                    "uuid": iface_uuid,
                    "instance_vm_id": vm_uuid
                }
                db_vm_iface_instance.update(db_vm_iface)
                if db_vm_iface_instance.get("ip_address"):  # increment ip_address
                    ip = db_vm_iface_instance.get("ip_address")
                    i = ip.rfind(".")
                    if i > 0:
                        try:
                            i += 1
                            ip = ip[i:] + str(int(ip[:i]) + 1)
                            db_vm_iface_instance["ip_address"] = ip
                        except:
                            db_vm_iface_instance["ip_address"] = None
                db_instance_interfaces.append(db_vm_iface_instance)
                myVMDict['networks'][iface_index]["uuid"] = iface_uuid
                iface_index += 1

            db_vim_action = {
                "instance_action_id": instance_action_id,
                "task_index": task_index,
                "datacenter_vim_id": myvim_thread_id,
                "action": "CREATE",
                "status": "SCHEDULED",
                "item": "instance_vms",
                "item_id": vm_uuid,
                "related": vm_uuid,
                "extra": yaml.safe_dump({"params": task_params, "depends_on": task_depends_on},
                                        default_flow_style=True, width=256)
            }
            task_index += 1
            db_vim_actions.append(db_vim_action)
    params_out["task_index"] = task_index
    params_out["uuid_list"] = uuid_list


def delete_instance(mydb, tenant_id, instance_id):
    # print "Checking that the instance_id exists and getting the instance dictionary"
    instanceDict = mydb.get_instance_scenario(instance_id, tenant_id)
    # print yaml.safe_dump(instanceDict, indent=4, default_flow_style=False)
    tenant_id = instanceDict["tenant_id"]

    # --> WIM
    # We need to retrieve the WIM Actions now, before the instance_scenario is
    # deleted. The reason for that is that: ON CASCADE rules will delete the
    # instance_wim_nets record in the database
    wim_actions = wim_engine.delete_actions(instance_scenario_id=instance_id)
    # <-- WIM

    # print "Checking that nfvo_tenant_id exists and getting the VIM URI and the VIM tenant_id"
    # 1. Delete from Database
    message = mydb.delete_instance_scenario(instance_id, tenant_id)

    # 2. delete from VIM
    error_msg = ""
    myvims = {}
    myvim_threads = {}
    vimthread_affected = {}
    net2vm_dependencies = {}

    task_index = 0
    instance_action_id = get_task_id()
    db_vim_actions = []
    db_instance_action = {
        "uuid": instance_action_id,   # same uuid for the instance and the action on create
        "tenant_id": tenant_id,
        "instance_id": instance_id,
        "description": "DELETE",
        # "number_tasks": 0 # filled bellow
    }

    # 2.1 deleting VNFFGs
    for sfp in instanceDict.get('sfps', ()):
        vimthread_affected[sfp["datacenter_tenant_id"]] = None
        datacenter_key = (sfp["datacenter_id"], sfp["datacenter_tenant_id"])
        if datacenter_key not in myvims:
            try:
                _, myvim_thread = get_vim_thread(mydb, tenant_id, sfp["datacenter_id"], sfp["datacenter_tenant_id"])
            except NfvoException as e:
                logger.error(str(e))
                myvim_thread = None
            myvim_threads[datacenter_key] = myvim_thread
            vims = get_vim(mydb, tenant_id, datacenter_id=sfp["datacenter_id"],
                           datacenter_tenant_id=sfp["datacenter_tenant_id"])
            if len(vims) == 0:
                logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(sfp["datacenter_id"], sfp["datacenter_tenant_id"]))
                myvims[datacenter_key] = None
            else:
                myvims[datacenter_key] = vims.values()[0]
        myvim = myvims[datacenter_key]
        myvim_thread = myvim_threads[datacenter_key]

        if not myvim:
            error_msg += "\n    vim_sfp_id={} cannot be deleted because datacenter={} not found".format(sfp['vim_sfp_id'], sfp["datacenter_id"])
            continue
        extra = {"params": (sfp['vim_sfp_id'])}
        db_vim_action = {
            "instance_action_id": instance_action_id,
            "task_index": task_index,
            "datacenter_vim_id": sfp["datacenter_tenant_id"],
            "action": "DELETE",
            "status": "SCHEDULED",
            "item": "instance_sfps",
            "item_id": sfp["uuid"],
            "related": sfp["related"],
            "extra": yaml.safe_dump(extra, default_flow_style=True, width=256)
        }
        task_index += 1
        db_vim_actions.append(db_vim_action)

    for classification in instanceDict['classifications']:
        vimthread_affected[classification["datacenter_tenant_id"]] = None
        datacenter_key = (classification["datacenter_id"], classification["datacenter_tenant_id"])
        if datacenter_key not in myvims:
            try:
                _, myvim_thread = get_vim_thread(mydb, tenant_id, classification["datacenter_id"], classification["datacenter_tenant_id"])
            except NfvoException as e:
                logger.error(str(e))
                myvim_thread = None
            myvim_threads[datacenter_key] = myvim_thread
            vims = get_vim(mydb, tenant_id, datacenter_id=classification["datacenter_id"],
                           datacenter_tenant_id=classification["datacenter_tenant_id"])
            if len(vims) == 0:
                logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(classification["datacenter_id"],
                                                                                               classification["datacenter_tenant_id"]))
                myvims[datacenter_key] = None
            else:
                myvims[datacenter_key] = vims.values()[0]
        myvim = myvims[datacenter_key]
        myvim_thread = myvim_threads[datacenter_key]

        if not myvim:
            error_msg += "\n    vim_classification_id={} cannot be deleted because datacenter={} not found".format(classification['vim_classification_id'],
                                                                                                                   classification["datacenter_id"])
            continue
        depends_on = [action["task_index"] for action in db_vim_actions if action["item"] == "instance_sfps"]
        extra = {"params": (classification['vim_classification_id']), "depends_on": depends_on}
        db_vim_action = {
            "instance_action_id": instance_action_id,
            "task_index": task_index,
            "datacenter_vim_id": classification["datacenter_tenant_id"],
            "action": "DELETE",
            "status": "SCHEDULED",
            "item": "instance_classifications",
            "item_id": classification["uuid"],
            "related": classification["related"],
            "extra": yaml.safe_dump(extra, default_flow_style=True, width=256)
        }
        task_index += 1
        db_vim_actions.append(db_vim_action)

    for sf in instanceDict.get('sfs', ()):
        vimthread_affected[sf["datacenter_tenant_id"]] = None
        datacenter_key = (sf["datacenter_id"], sf["datacenter_tenant_id"])
        if datacenter_key not in myvims:
            try:
                _, myvim_thread = get_vim_thread(mydb, tenant_id, sf["datacenter_id"], sf["datacenter_tenant_id"])
            except NfvoException as e:
                logger.error(str(e))
                myvim_thread = None
            myvim_threads[datacenter_key] = myvim_thread
            vims = get_vim(mydb, tenant_id, datacenter_id=sf["datacenter_id"],
                           datacenter_tenant_id=sf["datacenter_tenant_id"])
            if len(vims) == 0:
                logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(sf["datacenter_id"], sf["datacenter_tenant_id"]))
                myvims[datacenter_key] = None
            else:
                myvims[datacenter_key] = vims.values()[0]
        myvim = myvims[datacenter_key]
        myvim_thread = myvim_threads[datacenter_key]

        if not myvim:
            error_msg += "\n    vim_sf_id={} cannot be deleted because datacenter={} not found".format(sf['vim_sf_id'], sf["datacenter_id"])
            continue
        depends_on = [action["task_index"] for action in db_vim_actions if action["item"] == "instance_sfps"]
        extra = {"params": (sf['vim_sf_id']), "depends_on": depends_on}
        db_vim_action = {
            "instance_action_id": instance_action_id,
            "task_index": task_index,
            "datacenter_vim_id": sf["datacenter_tenant_id"],
            "action": "DELETE",
            "status": "SCHEDULED",
            "item": "instance_sfs",
            "item_id": sf["uuid"],
            "related": sf["related"],
            "extra": yaml.safe_dump(extra, default_flow_style=True, width=256)
        }
        task_index += 1
        db_vim_actions.append(db_vim_action)

    for sfi in instanceDict.get('sfis', ()):
        vimthread_affected[sfi["datacenter_tenant_id"]] = None
        datacenter_key = (sfi["datacenter_id"], sfi["datacenter_tenant_id"])
        if datacenter_key not in myvims:
            try:
                _, myvim_thread = get_vim_thread(mydb, tenant_id, sfi["datacenter_id"], sfi["datacenter_tenant_id"])
            except NfvoException as e:
                logger.error(str(e))
                myvim_thread = None
            myvim_threads[datacenter_key] = myvim_thread
            vims = get_vim(mydb, tenant_id, datacenter_id=sfi["datacenter_id"],
                           datacenter_tenant_id=sfi["datacenter_tenant_id"])
            if len(vims) == 0:
                logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(sfi["datacenter_id"], sfi["datacenter_tenant_id"]))
                myvims[datacenter_key] = None
            else:
                myvims[datacenter_key] = vims.values()[0]
        myvim = myvims[datacenter_key]
        myvim_thread = myvim_threads[datacenter_key]

        if not myvim:
            error_msg += "\n    vim_sfi_id={} cannot be deleted because datacenter={} not found".format(sfi['vim_sfi_id'], sfi["datacenter_id"])
            continue
        depends_on = [action["task_index"] for action in db_vim_actions if action["item"] == "instance_sfs"]
        extra = {"params": (sfi['vim_sfi_id']), "depends_on": depends_on}
        db_vim_action = {
            "instance_action_id": instance_action_id,
            "task_index": task_index,
            "datacenter_vim_id": sfi["datacenter_tenant_id"],
            "action": "DELETE",
            "status": "SCHEDULED",
            "item": "instance_sfis",
            "item_id": sfi["uuid"],
            "related": sfi["related"],
            "extra": yaml.safe_dump(extra, default_flow_style=True, width=256)
        }
        task_index += 1
        db_vim_actions.append(db_vim_action)

    # 2.2 deleting VMs
    # vm_fail_list=[]
    for sce_vnf in instanceDict.get('vnfs', ()):
        datacenter_key = (sce_vnf["datacenter_id"], sce_vnf["datacenter_tenant_id"])
        vimthread_affected[sce_vnf["datacenter_tenant_id"]] = None
        if datacenter_key not in myvims:
            try:
                _, myvim_thread = get_vim_thread(mydb, tenant_id, sce_vnf["datacenter_id"], sce_vnf["datacenter_tenant_id"])
            except NfvoException as e:
                logger.error(str(e))
                myvim_thread = None
            myvim_threads[datacenter_key] = myvim_thread
            vims = get_vim(mydb, tenant_id, datacenter_id=sce_vnf["datacenter_id"],
                           datacenter_tenant_id=sce_vnf["datacenter_tenant_id"])
            if len(vims) == 0:
                logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(sce_vnf["datacenter_id"],
                                                                                               sce_vnf["datacenter_tenant_id"]))
                myvims[datacenter_key] = None
            else:
                myvims[datacenter_key] = vims.values()[0]
        myvim = myvims[datacenter_key]
        myvim_thread = myvim_threads[datacenter_key]

        for vm in sce_vnf['vms']:
            if not myvim:
                error_msg += "\n    VM id={} cannot be deleted because datacenter={} not found".format(vm['vim_vm_id'], sce_vnf["datacenter_id"])
                continue
            sfi_dependencies = [action["task_index"] for action in db_vim_actions if action["item"] == "instance_sfis"]
            db_vim_action = {
                "instance_action_id": instance_action_id,
                "task_index": task_index,
                "datacenter_vim_id": sce_vnf["datacenter_tenant_id"],
                "action": "DELETE",
                "status": "SCHEDULED",
                "item": "instance_vms",
                "item_id": vm["uuid"],
                "related": vm["related"],
                "extra": yaml.safe_dump({"params": vm["interfaces"], "depends_on": sfi_dependencies},
                                        default_flow_style=True, width=256)
            }
            db_vim_actions.append(db_vim_action)
            for interface in vm["interfaces"]:
                if not interface.get("instance_net_id"):
                    continue
                if interface["instance_net_id"] not in net2vm_dependencies:
                    net2vm_dependencies[interface["instance_net_id"]] = []
                net2vm_dependencies[interface["instance_net_id"]].append(task_index)
            task_index += 1

    # 2.3 deleting NETS
    # net_fail_list=[]
    for net in instanceDict['nets']:
        vimthread_affected[net["datacenter_tenant_id"]] = None
        datacenter_key = (net["datacenter_id"], net["datacenter_tenant_id"])
        if datacenter_key not in myvims:
            try:
                _,myvim_thread = get_vim_thread(mydb, tenant_id, net["datacenter_id"], net["datacenter_tenant_id"])
            except NfvoException as e:
                logger.error(str(e))
                myvim_thread = None
            myvim_threads[datacenter_key] = myvim_thread
            vims = get_vim(mydb, tenant_id, datacenter_id=net["datacenter_id"],
                           datacenter_tenant_id=net["datacenter_tenant_id"])
            if len(vims) == 0:
                logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(net["datacenter_id"], net["datacenter_tenant_id"]))
                myvims[datacenter_key] = None
            else:
                myvims[datacenter_key] = vims.values()[0]
        myvim = myvims[datacenter_key]
        myvim_thread = myvim_threads[datacenter_key]

        if not myvim:
            error_msg += "\n    Net VIM_id={} cannot be deleted because datacenter={} not found".format(net['vim_net_id'], net["datacenter_id"])
            continue
        extra = {"params": (net['vim_net_id'], net['sdn_net_id'])}
        if net2vm_dependencies.get(net["uuid"]):
            extra["depends_on"] = net2vm_dependencies[net["uuid"]]
        sfi_dependencies = [action["task_index"] for action in db_vim_actions if action["item"] == "instance_sfis"]
        if len(sfi_dependencies) > 0:
            if "depends_on" in extra:
                extra["depends_on"] += sfi_dependencies
            else:
                extra["depends_on"] = sfi_dependencies
        db_vim_action = {
            "instance_action_id": instance_action_id,
            "task_index": task_index,
            "datacenter_vim_id": net["datacenter_tenant_id"],
            "action": "DELETE",
            "status": "SCHEDULED",
            "item": "instance_nets",
            "item_id": net["uuid"],
            "related": net["related"],
            "extra": yaml.safe_dump(extra, default_flow_style=True, width=256)
        }
        task_index += 1
        db_vim_actions.append(db_vim_action)

    db_instance_action["number_tasks"] = task_index

    # --> WIM
    wim_actions, db_instance_action = (
        wim_engine.incorporate_actions(wim_actions, db_instance_action))
    # <-- WIM

    db_tables = [
        {"instance_actions": db_instance_action},
        {"vim_wim_actions": db_vim_actions + wim_actions}
    ]

    logger.debug("delete_instance done DB tables: %s",
                 yaml.safe_dump(db_tables, indent=4, default_flow_style=False))
    mydb.new_rows(db_tables, ())
    for myvim_thread_id in vimthread_affected.keys():
        vim_threads["running"][myvim_thread_id].insert_task(db_vim_actions)

    wim_engine.dispatch(wim_actions)

    if len(error_msg) > 0:
        return 'action_id={} instance {} deleted but some elements could not be deleted, or already deleted '\
               '(error: 404) from VIM: {}'.format(instance_action_id, message, error_msg)
    else:
        return "action_id={} instance {} deleted".format(instance_action_id, message)

def get_instance_id(mydb, tenant_id, instance_id):
    global ovim
    #check valid tenant_id
    check_tenant(mydb, tenant_id)
    #obtain data

    instance_dict = mydb.get_instance_scenario(instance_id, tenant_id, verbose=True)
    for net in instance_dict["nets"]:
        if net.get("sdn_net_id"):
            net_sdn = ovim.show_network(net["sdn_net_id"])
            net["sdn_info"] = {
                "admin_state_up": net_sdn.get("admin_state_up"),
                "flows": net_sdn.get("flows"),
                "last_error": net_sdn.get("last_error"),
                "ports": net_sdn.get("ports"),
                "type": net_sdn.get("type"),
                "status": net_sdn.get("status"),
                "vlan": net_sdn.get("vlan"),
            }
    return instance_dict

@deprecated("Instance is automatically refreshed by vim_threads")
def refresh_instance(mydb, nfvo_tenant, instanceDict, datacenter=None, vim_tenant=None):
    '''Refreshes a scenario instance. It modifies instanceDict'''
    '''Returns:
         - result: <0 if there is any unexpected error, n>=0 if no errors where n is the number of vms and nets that couldn't be updated in the database
         - error_msg
    '''
    # # Assumption: nfvo_tenant and instance_id were checked before entering into this function
    # #print "nfvo.refresh_instance begins"
    # #print json.dumps(instanceDict, indent=4)
    #
    # #print "Getting the VIM URL and the VIM tenant_id"
    # myvims={}
    #
    # # 1. Getting VIM vm and net list
    # vms_updated = [] #List of VM instance uuids in openmano that were updated
    # vms_notupdated=[]
    # vm_list = {}
    # for sce_vnf in instanceDict['vnfs']:
    #     datacenter_key = (sce_vnf["datacenter_id"], sce_vnf["datacenter_tenant_id"])
    #     if datacenter_key not in vm_list:
    #         vm_list[datacenter_key] = []
    #     if datacenter_key not in myvims:
    #         vims = get_vim(mydb, nfvo_tenant, datacenter_id=sce_vnf["datacenter_id"],
    #                        datacenter_tenant_id=sce_vnf["datacenter_tenant_id"])
    #         if len(vims) == 0:
    #             logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(sce_vnf["datacenter_id"], sce_vnf["datacenter_tenant_id"]))
    #             myvims[datacenter_key] = None
    #         else:
    #             myvims[datacenter_key] = vims.values()[0]
    #     for vm in sce_vnf['vms']:
    #         vm_list[datacenter_key].append(vm['vim_vm_id'])
    #         vms_notupdated.append(vm["uuid"])
    #
    # nets_updated = [] #List of VM instance uuids in openmano that were updated
    # nets_notupdated=[]
    # net_list = {}
    # for net in instanceDict['nets']:
    #     datacenter_key = (net["datacenter_id"], net["datacenter_tenant_id"])
    #     if datacenter_key not in net_list:
    #         net_list[datacenter_key] = []
    #     if datacenter_key not in myvims:
    #         vims = get_vim(mydb, nfvo_tenant, datacenter_id=net["datacenter_id"],
    #                        datacenter_tenant_id=net["datacenter_tenant_id"])
    #         if len(vims) == 0:
    #             logger.error("datacenter '{}' with datacenter_tenant_id '{}' not found".format(net["datacenter_id"], net["datacenter_tenant_id"]))
    #             myvims[datacenter_key] = None
    #         else:
    #             myvims[datacenter_key] = vims.values()[0]
    #
    #     net_list[datacenter_key].append(net['vim_net_id'])
    #     nets_notupdated.append(net["uuid"])
    #
    # # 1. Getting the status of all VMs
    # vm_dict={}
    # for datacenter_key in myvims:
    #     if not vm_list.get(datacenter_key):
    #         continue
    #     failed = True
    #     failed_message=""
    #     if not myvims[datacenter_key]:
    #         failed_message = "datacenter '{}' with datacenter_tenant_id '{}' not found".format(net["datacenter_id"], net["datacenter_tenant_id"])
    #     else:
    #         try:
    #             vm_dict.update(myvims[datacenter_key].refresh_vms_status(vm_list[datacenter_key]) )
    #             failed = False
    #         except vimconn.vimconnException as e:
    #             logger.error("VIM exception %s %s", type(e).__name__, str(e))
    #             failed_message = str(e)
    #     if failed:
    #         for vm in vm_list[datacenter_key]:
    #             vm_dict[vm] = {'status': "VIM_ERROR", 'error_msg': failed_message}
    #
    # # 2. Update the status of VMs in the instanceDict, while collects the VMs whose status changed
    # for sce_vnf in instanceDict['vnfs']:
    #     for vm in sce_vnf['vms']:
    #         vm_id = vm['vim_vm_id']
    #         interfaces = vm_dict[vm_id].pop('interfaces', [])
    #         #2.0 look if contain manamgement interface, and if not change status from ACTIVE:NoMgmtIP to ACTIVE
    #         has_mgmt_iface = False
    #         for iface in vm["interfaces"]:
    #             if iface["type"]=="mgmt":
    #                 has_mgmt_iface = True
    #         if vm_dict[vm_id]['status'] == "ACTIVE:NoMgmtIP" and not has_mgmt_iface:
    #             vm_dict[vm_id]['status'] = "ACTIVE"
    #         if vm_dict[vm_id].get('error_msg') and len(vm_dict[vm_id]['error_msg']) >= 1024:
    #             vm_dict[vm_id]['error_msg'] = vm_dict[vm_id]['error_msg'][:516] + " ... " + vm_dict[vm_id]['error_msg'][-500:]
    #         if vm['status'] != vm_dict[vm_id]['status'] or vm.get('error_msg')!=vm_dict[vm_id].get('error_msg') or vm.get('vim_info')!=vm_dict[vm_id].get('vim_info'):
    #             vm['status']    = vm_dict[vm_id]['status']
    #             vm['error_msg'] = vm_dict[vm_id].get('error_msg')
    #             vm['vim_info']  = vm_dict[vm_id].get('vim_info')
    #             # 2.1. Update in openmano DB the VMs whose status changed
    #             try:
    #                 updates = mydb.update_rows('instance_vms', UPDATE=vm_dict[vm_id], WHERE={'uuid':vm["uuid"]})
    #                 vms_notupdated.remove(vm["uuid"])
    #                 if updates>0:
    #                     vms_updated.append(vm["uuid"])
    #             except db_base_Exception as e:
    #                 logger.error("nfvo.refresh_instance error database update: %s", str(e))
    #         # 2.2. Update in openmano DB the interface VMs
    #         for interface in interfaces:
    #             #translate from vim_net_id to instance_net_id
    #             network_id_list=[]
    #             for net in instanceDict['nets']:
    #                 if net["vim_net_id"] == interface["vim_net_id"]:
    #                     network_id_list.append(net["uuid"])
    #             if not network_id_list:
    #                 continue
    #             del interface["vim_net_id"]
    #             try:
    #                 for network_id in network_id_list:
    #                     mydb.update_rows('instance_interfaces', UPDATE=interface, WHERE={'instance_vm_id':vm["uuid"], "instance_net_id":network_id})
    #             except db_base_Exception as e:
    #                 logger.error( "nfvo.refresh_instance error with vm=%s, interface_net_id=%s", vm["uuid"], network_id)
    #
    # # 3. Getting the status of all nets
    # net_dict = {}
    # for datacenter_key in myvims:
    #     if not net_list.get(datacenter_key):
    #         continue
    #     failed = True
    #     failed_message = ""
    #     if not myvims[datacenter_key]:
    #         failed_message = "datacenter '{}' with datacenter_tenant_id '{}' not found".format(net["datacenter_id"], net["datacenter_tenant_id"])
    #     else:
    #         try:
    #             net_dict.update(myvims[datacenter_key].refresh_nets_status(net_list[datacenter_key]) )
    #             failed = False
    #         except vimconn.vimconnException as e:
    #             logger.error("VIM exception %s %s", type(e).__name__, str(e))
    #             failed_message = str(e)
    #     if failed:
    #         for net in net_list[datacenter_key]:
    #             net_dict[net] = {'status': "VIM_ERROR", 'error_msg': failed_message}
    #
    # # 4. Update the status of nets in the instanceDict, while collects the nets whose status changed
    # # TODO: update nets inside a vnf
    # for net in instanceDict['nets']:
    #     net_id = net['vim_net_id']
    #     if net_dict[net_id].get('error_msg') and len(net_dict[net_id]['error_msg']) >= 1024:
    #         net_dict[net_id]['error_msg'] = net_dict[net_id]['error_msg'][:516] + " ... " + net_dict[vm_id]['error_msg'][-500:]
    #     if net['status'] != net_dict[net_id]['status'] or net.get('error_msg')!=net_dict[net_id].get('error_msg') or net.get('vim_info')!=net_dict[net_id].get('vim_info'):
    #         net['status']    = net_dict[net_id]['status']
    #         net['error_msg'] = net_dict[net_id].get('error_msg')
    #         net['vim_info']  = net_dict[net_id].get('vim_info')
    #         # 5.1. Update in openmano DB the nets whose status changed
    #         try:
    #             updated = mydb.update_rows('instance_nets', UPDATE=net_dict[net_id], WHERE={'uuid':net["uuid"]})
    #             nets_notupdated.remove(net["uuid"])
    #             if updated>0:
    #                 nets_updated.append(net["uuid"])
    #         except db_base_Exception as e:
    #             logger.error("nfvo.refresh_instance error database update: %s", str(e))
    #
    # # Returns appropriate output
    # #print "nfvo.refresh_instance finishes"
    # logger.debug("VMs updated in the database: %s; nets updated in the database %s; VMs not updated: %s; nets not updated: %s",
    #             str(vms_updated), str(nets_updated), str(vms_notupdated), str(nets_notupdated))
    instance_id = instanceDict['uuid']
    # if len(vms_notupdated)+len(nets_notupdated)>0:
    #     error_msg = "VMs not updated: " + str(vms_notupdated) + "; nets not updated: " + str(nets_notupdated)
    #     return len(vms_notupdated)+len(nets_notupdated), 'Scenario instance ' + instance_id + ' refreshed but some elements could not be updated in the database: ' + error_msg

    return 0, 'Scenario instance ' + instance_id + ' refreshed.'

def instance_action(mydb,nfvo_tenant,instance_id, action_dict):
    #print "Checking that the instance_id exists and getting the instance dictionary"
    instanceDict = mydb.get_instance_scenario(instance_id, nfvo_tenant)
    #print yaml.safe_dump(instanceDict, indent=4, default_flow_style=False)

    #print "Checking that nfvo_tenant_id exists and getting the VIM URI and the VIM tenant_id"
    vims = get_vim(mydb, nfvo_tenant, instanceDict['datacenter_id'])
    if len(vims) == 0:
        raise NfvoException("datacenter '{}' not found".format(str(instanceDict['datacenter_id'])), httperrors.Not_Found)
    myvim = vims.values()[0]
    vm_result = {}
    vm_error = 0
    vm_ok = 0

    myvim_threads_id = {}
    if action_dict.get("vdu-scaling"):
        db_instance_vms = []
        db_vim_actions = []
        db_instance_interfaces = []
        instance_action_id = get_task_id()
        db_instance_action = {
            "uuid": instance_action_id,   # same uuid for the instance and the action on create
            "tenant_id": nfvo_tenant,
            "instance_id": instance_id,
            "description": "SCALE",
        }
        vm_result["instance_action_id"] = instance_action_id
        vm_result["created"] = []
        vm_result["deleted"] = []
        task_index = 0
        for vdu in action_dict["vdu-scaling"]:
            vdu_id = vdu.get("vdu-id")
            osm_vdu_id = vdu.get("osm_vdu_id")
            member_vnf_index = vdu.get("member-vnf-index")
            vdu_count = vdu.get("count", 1)
            if vdu_id:
                target_vms = mydb.get_rows(
                    FROM="instance_vms as vms join instance_vnfs as vnfs on vms.instance_vnf_id=vnfs.uuid",
                    WHERE={"vms.uuid": vdu_id},
                    ORDER_BY="vms.created_at"
                )
                if not target_vms:
                    raise NfvoException("Cannot find the vdu with id {}".format(vdu_id), httperrors.Not_Found)
            else:
                if not osm_vdu_id and not member_vnf_index:
                    raise NfvoException("Invalid input vdu parameters. Must supply either 'vdu-id' of 'osm_vdu_id','member-vnf-index'")
                target_vms = mydb.get_rows(
                    # SELECT=("ivms.uuid", "ivnfs.datacenter_id", "ivnfs.datacenter_tenant_id"),
                    FROM="instance_vms as ivms join instance_vnfs as ivnfs on ivms.instance_vnf_id=ivnfs.uuid"\
                         " join sce_vnfs as svnfs on ivnfs.sce_vnf_id=svnfs.uuid"\
                         " join vms on ivms.vm_id=vms.uuid",
                    WHERE={"vms.osm_id": osm_vdu_id, "svnfs.member_vnf_index": member_vnf_index,
                           "ivnfs.instance_scenario_id": instance_id},
                    ORDER_BY="ivms.created_at"
                )
                if not target_vms:
                    raise NfvoException("Cannot find the vdu with osm_vdu_id {} and member-vnf-index {}".format(osm_vdu_id, member_vnf_index), httperrors.Not_Found)
                vdu_id = target_vms[-1]["uuid"]
            target_vm = target_vms[-1]
            datacenter = target_vm["datacenter_id"]
            myvim_threads_id[datacenter], _ = get_vim_thread(mydb, nfvo_tenant, datacenter)

            if vdu["type"] == "delete":
                for index in range(0, vdu_count):
                    target_vm = target_vms[-1-index]
                    vdu_id = target_vm["uuid"]
                    # look for nm
                    vm_interfaces = None
                    for sce_vnf in instanceDict['vnfs']:
                        for vm in sce_vnf['vms']:
                            if vm["uuid"] == vdu_id:
                                # TODO revise this should not be vm["uuid"]   instance_vms["vm_id"]
                                vm_interfaces = vm["interfaces"]
                                break

                    db_vim_action = {
                        "instance_action_id": instance_action_id,
                        "task_index": task_index,
                        "datacenter_vim_id": target_vm["datacenter_tenant_id"],
                        "action": "DELETE",
                        "status": "SCHEDULED",
                        "item": "instance_vms",
                        "item_id": vdu_id,
                        "related": target_vm["related"],
                        "extra": yaml.safe_dump({"params": vm_interfaces},
                                                default_flow_style=True, width=256)
                    }
                    task_index += 1
                    db_vim_actions.append(db_vim_action)
                    vm_result["deleted"].append(vdu_id)
                    # delete from database
                    db_instance_vms.append({"TO-DELETE": vdu_id})

            else:  # vdu["type"] == "create":
                iface2iface = {}
                where = {"item": "instance_vms", "item_id": target_vm["uuid"], "action": "CREATE"}

                vim_action_to_clone = mydb.get_rows(FROM="vim_wim_actions", WHERE=where)
                if not vim_action_to_clone:
                    raise NfvoException("Cannot find the vim_action at database with {}".format(where), httperrors.Internal_Server_Error)
                vim_action_to_clone = vim_action_to_clone[0]
                extra = yaml.safe_load(vim_action_to_clone["extra"])

                # generate a new depends_on. Convert format TASK-Y into new format TASK-ACTION-XXXX.XXXX.Y
                # TODO do the same for flavor and image when available
                task_depends_on = []
                task_params = extra["params"]
                task_params_networks = deepcopy(task_params[5])
                for iface in task_params[5]:
                    if iface["net_id"].startswith("TASK-"):
                        if "." not in iface["net_id"]:
                            task_depends_on.append("{}.{}".format(vim_action_to_clone["instance_action_id"],
                                                             iface["net_id"][5:]))
                            iface["net_id"] = "TASK-{}.{}".format(vim_action_to_clone["instance_action_id"],
                                                                  iface["net_id"][5:])
                        else:
                            task_depends_on.append(iface["net_id"][5:])
                    if "mac_address" in iface:
                        del iface["mac_address"]

                vm_ifaces_to_clone = mydb.get_rows(FROM="instance_interfaces", WHERE={"instance_vm_id": target_vm["uuid"]})
                for index in range(0, vdu_count):
                    vm_uuid = str(uuid4())
                    vm_name = target_vm.get('vim_name')
                    try:
                        suffix = vm_name.rfind("-")
                        vm_name = vm_name[:suffix+1] + str(index + 1 + int(vm_name[suffix+1:]))
                    except Exception:
                        pass
                    db_instance_vm = {
                        "uuid": vm_uuid,
                        'related': vm_uuid,
                        'instance_vnf_id': target_vm['instance_vnf_id'],
                        'vm_id': target_vm['vm_id'],
                        'vim_name': vm_name,
                    }
                    db_instance_vms.append(db_instance_vm)

                    for vm_iface in vm_ifaces_to_clone:
                        iface_uuid = str(uuid4())
                        iface2iface[vm_iface["uuid"]] = iface_uuid
                        db_vm_iface = {
                            "uuid": iface_uuid,
                            'instance_vm_id': vm_uuid,
                            "instance_net_id": vm_iface["instance_net_id"],
                            'interface_id': vm_iface['interface_id'],
                            'type': vm_iface['type'],
                            'floating_ip': vm_iface['floating_ip'],
                            'port_security': vm_iface['port_security']
                        }
                        db_instance_interfaces.append(db_vm_iface)
                    task_params_copy = deepcopy(task_params)
                    for iface in task_params_copy[5]:
                        iface["uuid"] = iface2iface[iface["uuid"]]
                        # increment ip_address
                        if "ip_address" in iface:
                            ip = iface.get("ip_address")
                            i = ip.rfind(".")
                            if i > 0:
                                try:
                                    i += 1
                                    ip = ip[i:] + str(int(ip[:i]) + 1)
                                    iface["ip_address"] = ip
                                except:
                                    iface["ip_address"] = None
                    if vm_name:
                        task_params_copy[0] = vm_name
                    db_vim_action = {
                        "instance_action_id": instance_action_id,
                        "task_index": task_index,
                        "datacenter_vim_id": vim_action_to_clone["datacenter_vim_id"],
                        "action": "CREATE",
                        "status": "SCHEDULED",
                        "item": "instance_vms",
                        "item_id": vm_uuid,
                        "related": vm_uuid,
                        # ALF
                        # ALF
                        # TODO examinar parametros, quitar MAC o incrementar. Incrementar IP y colocar las dependencias con ACTION-asdfasd.
                        # ALF
                        # ALF
                        "extra": yaml.safe_dump({"params": task_params_copy, "depends_on": task_depends_on}, default_flow_style=True, width=256)
                    }
                    task_index += 1
                    db_vim_actions.append(db_vim_action)
                    vm_result["created"].append(vm_uuid)

        db_instance_action["number_tasks"] = task_index
        db_tables = [
            {"instance_vms": db_instance_vms},
            {"instance_interfaces": db_instance_interfaces},
            {"instance_actions": db_instance_action},
            # TODO revise sfps
            # {"instance_sfis": db_instance_sfis},
            # {"instance_sfs": db_instance_sfs},
            # {"instance_classifications": db_instance_classifications},
            # {"instance_sfps": db_instance_sfps},
            {"vim_wim_actions": db_vim_actions}
        ]
        logger.debug("create_vdu done DB tables: %s",
                     yaml.safe_dump(db_tables, indent=4, default_flow_style=False))
        mydb.new_rows(db_tables, [])
        for myvim_thread in myvim_threads_id.values():
            vim_threads["running"][myvim_thread].insert_task(db_vim_actions)

        return vm_result

    input_vnfs = action_dict.pop("vnfs", [])
    input_vms = action_dict.pop("vms", [])
    action_over_all = True if not input_vnfs and not input_vms else False
    for sce_vnf in instanceDict['vnfs']:
        for vm in sce_vnf['vms']:
            if not action_over_all and sce_vnf['uuid'] not in input_vnfs and sce_vnf['vnf_name'] not in input_vnfs and \
                    sce_vnf['member_vnf_index'] not in input_vnfs and \
                    vm['uuid'] not in input_vms and vm['name'] not in input_vms:
                continue
            try:
                if "add_public_key" in action_dict:
                    mgmt_access = {}
                    if sce_vnf.get('mgmt_access'):
                        mgmt_access = yaml.load(sce_vnf['mgmt_access'])
                        ssh_access = mgmt_access['config-access']['ssh-access']
                        tenant = mydb.get_rows_by_id('nfvo_tenants', nfvo_tenant)
                        try:
                            if ssh_access['required'] and ssh_access['default-user']:
                                if 'ip_address' in vm:
                                    mgmt_ip = vm['ip_address'].split(';')
                                    password = mgmt_access['config-access'].get('password')
                                    priv_RO_key = decrypt_key(tenant[0]['encrypted_RO_priv_key'], tenant[0]['uuid'])
                                    myvim.inject_user_key(mgmt_ip[0], ssh_access['default-user'],
                                                          action_dict['add_public_key'],
                                                          password=password, ro_key=priv_RO_key)
                            else:
                                raise NfvoException("Unable to inject ssh key in vm: {} - Aborting".format(vm['uuid']),
                                                    httperrors.Internal_Server_Error)
                        except KeyError:
                            raise NfvoException("Unable to inject ssh key in vm: {} - Aborting".format(vm['uuid']),
                                                httperrors.Internal_Server_Error)
                    else:
                        raise NfvoException("Unable to inject ssh key in vm: {} - Aborting".format(vm['uuid']),
                                            httperrors.Internal_Server_Error)
                else:
                    data = myvim.action_vminstance(vm['vim_vm_id'], action_dict)
                    if "console" in action_dict:
                        if not global_config["http_console_proxy"]:
                            vm_result[ vm['uuid'] ] = {"vim_result": 200,
                                                       "description": "{protocol}//{ip}:{port}/{suffix}".format(
                                                                                    protocol=data["protocol"],
                                                                                    ip = data["server"],
                                                                                    port = data["port"],
                                                                                    suffix = data["suffix"]),
                                                       "name":vm['name']
                                                    }
                            vm_ok +=1
                        elif data["server"]=="127.0.0.1" or data["server"]=="localhost":
                            vm_result[ vm['uuid'] ] = {"vim_result": -httperrors.Unauthorized,
                                                       "description": "this console is only reachable by local interface",
                                                       "name":vm['name']
                                                    }
                            vm_error+=1
                        else:
                        #print "console data", data
                            try:
                                console_thread = create_or_use_console_proxy_thread(data["server"], data["port"])
                                vm_result[ vm['uuid'] ] = {"vim_result": 200,
                                                           "description": "{protocol}//{ip}:{port}/{suffix}".format(
                                                                                        protocol=data["protocol"],
                                                                                        ip = global_config["http_console_host"],
                                                                                        port = console_thread.port,
                                                                                        suffix = data["suffix"]),
                                                           "name":vm['name']
                                                        }
                                vm_ok +=1
                            except NfvoException as e:
                                vm_result[ vm['uuid'] ] = {"vim_result": e.http_code, "name":vm['name'], "description": str(e)}
                                vm_error+=1

                    else:
                        vm_result[ vm['uuid'] ] = {"vim_result": 200, "description": "ok", "name":vm['name']}
                        vm_ok +=1
            except vimconn.vimconnException as e:
                vm_result[ vm['uuid'] ] = {"vim_result": e.http_code, "name":vm['name'], "description": str(e)}
                vm_error+=1

    if vm_ok==0: #all goes wrong
        return vm_result
    else:
        return vm_result

def instance_action_get(mydb, nfvo_tenant, instance_id, action_id):
    filter = {}
    if nfvo_tenant and nfvo_tenant != "any":
        filter["tenant_id"] = nfvo_tenant
    if instance_id and instance_id != "any":
        filter["instance_id"] = instance_id
    if action_id:
        filter["uuid"] = action_id
    rows = mydb.get_rows(FROM="instance_actions", WHERE=filter)
    if action_id:
        if not rows:
            raise NfvoException("Not found any action with this criteria", httperrors.Not_Found)
        vim_wim_actions = mydb.get_rows(FROM="vim_wim_actions", WHERE={"instance_action_id": action_id})
        rows[0]["vim_wim_actions"] = vim_wim_actions
        # for backward compatibility set vim_actions = vim_wim_actions
        rows[0]["vim_actions"] = vim_wim_actions
    return {"actions": rows}


def create_or_use_console_proxy_thread(console_server, console_port):
    #look for a non-used port
    console_thread_key = console_server + ":" + str(console_port)
    if console_thread_key in global_config["console_thread"]:
        #global_config["console_thread"][console_thread_key].start_timeout()
        return global_config["console_thread"][console_thread_key]

    for port in  global_config["console_port_iterator"]():
        #print "create_or_use_console_proxy_thread() port:", port
        if port in global_config["console_ports"]:
            continue
        try:
            clithread = cli.ConsoleProxyThread(global_config['http_host'], port, console_server, console_port)
            clithread.start()
            global_config["console_thread"][console_thread_key] = clithread
            global_config["console_ports"][port] = console_thread_key
            return clithread
        except cli.ConsoleProxyExceptionPortUsed as e:
            #port used, try with onoher
            continue
        except cli.ConsoleProxyException as e:
            raise NfvoException(str(e), httperrors.Bad_Request)
    raise NfvoException("Not found any free 'http_console_ports'", httperrors.Conflict)


def check_tenant(mydb, tenant_id):
    '''check that tenant exists at database'''
    tenant = mydb.get_rows(FROM='nfvo_tenants', SELECT=('uuid',), WHERE={'uuid': tenant_id})
    if not tenant:
        raise NfvoException("tenant '{}' not found".format(tenant_id), httperrors.Not_Found)
    return

def new_tenant(mydb, tenant_dict):

    tenant_uuid = str(uuid4())
    tenant_dict['uuid'] = tenant_uuid
    try:
        pub_key, priv_key = create_RO_keypair(tenant_uuid)
        tenant_dict['RO_pub_key'] = pub_key
        tenant_dict['encrypted_RO_priv_key'] = priv_key
        mydb.new_row("nfvo_tenants", tenant_dict, confidential_data=True)
    except db_base_Exception as e:
        raise NfvoException("Error creating the new tenant: {} ".format(tenant_dict['name']) + str(e), e.http_code)
    return tenant_uuid

def delete_tenant(mydb, tenant):
    #get nfvo_tenant info

    tenant_dict = mydb.get_table_by_uuid_name('nfvo_tenants', tenant, 'tenant')
    mydb.delete_row_by_id("nfvo_tenants", tenant_dict['uuid'])
    return tenant_dict['uuid'] + " " + tenant_dict["name"]


def new_datacenter(mydb, datacenter_descriptor):
    sdn_port_mapping = None
    if "config" in datacenter_descriptor:
        sdn_port_mapping = datacenter_descriptor["config"].pop("sdn-port-mapping", None)
        datacenter_descriptor["config"] = yaml.safe_dump(datacenter_descriptor["config"], default_flow_style=True,
                                                         width=256)
    # Check that datacenter-type is correct
    datacenter_type = datacenter_descriptor.get("type", "openvim");
    # module_info = None
    try:
        module = "vimconn_" + datacenter_type
        pkg = __import__("osm_ro." + module)
        # vim_conn = getattr(pkg, module)
        # module_info = imp.find_module(module, [__file__[:__file__.rfind("/")]])
    except (IOError, ImportError):
        # if module_info and module_info[0]:
        #    file.close(module_info[0])
        raise NfvoException("Incorrect datacenter type '{}'. Plugin '{}.py' not installed".format(datacenter_type,
                                                                                                  module),
                            httperrors.Bad_Request)

    datacenter_id = mydb.new_row("datacenters", datacenter_descriptor, add_uuid=True, confidential_data=True)
    if sdn_port_mapping:
        try:
            datacenter_sdn_port_mapping_set(mydb, None, datacenter_id, sdn_port_mapping)
        except Exception as e:
            mydb.delete_row_by_id("datacenters", datacenter_id)   # Rollback
            raise e
    return datacenter_id


def edit_datacenter(mydb, datacenter_id_name, datacenter_descriptor):
    # obtain data, check that only one exist
    datacenter = mydb.get_table_by_uuid_name('datacenters', datacenter_id_name)

    # edit data
    datacenter_id = datacenter['uuid']
    where = {'uuid': datacenter['uuid']}
    remove_port_mapping = False
    new_sdn_port_mapping = None
    if "config" in datacenter_descriptor:
        if datacenter_descriptor['config'] != None:
            try:
                new_config_dict = datacenter_descriptor["config"]
                if "sdn-port-mapping" in new_config_dict:
                    remove_port_mapping = True
                    new_sdn_port_mapping = new_config_dict.pop("sdn-port-mapping")
                # delete null fields
                to_delete = []
                for k in new_config_dict:
                    if new_config_dict[k] is None:
                        to_delete.append(k)
                        if k == 'sdn-controller':
                            remove_port_mapping = True

                config_text = datacenter.get("config")
                if not config_text:
                    config_text = '{}'
                config_dict = yaml.load(config_text)
                config_dict.update(new_config_dict)
                # delete null fields
                for k in to_delete:
                    del config_dict[k]
            except Exception as e:
                raise NfvoException("Bad format at datacenter:config " + str(e), httperrors.Bad_Request)
        if config_dict:
            datacenter_descriptor["config"] = yaml.safe_dump(config_dict, default_flow_style=True, width=256)
        else:
            datacenter_descriptor["config"] = None
        if remove_port_mapping:
            try:
                datacenter_sdn_port_mapping_delete(mydb, None, datacenter_id)
            except ovimException as e:
                raise NfvoException("Error deleting datacenter-port-mapping " + str(e), httperrors.Conflict)

    mydb.update_rows('datacenters', datacenter_descriptor, where)
    if new_sdn_port_mapping:
        try:
            datacenter_sdn_port_mapping_set(mydb, None, datacenter_id, new_sdn_port_mapping)
        except ovimException as e:
            # Rollback
            mydb.update_rows('datacenters', datacenter, where)
            raise NfvoException("Error adding datacenter-port-mapping " + str(e), httperrors.Conflict)
    return datacenter_id


def delete_datacenter(mydb, datacenter):
    #get nfvo_tenant info
    datacenter_dict = mydb.get_table_by_uuid_name('datacenters', datacenter, 'datacenter')
    mydb.delete_row_by_id("datacenters", datacenter_dict['uuid'])
    try:
        datacenter_sdn_port_mapping_delete(mydb, None, datacenter_dict['uuid'])
    except ovimException as e:
        raise NfvoException("Error deleting datacenter-port-mapping " + str(e))
    return datacenter_dict['uuid'] + " " + datacenter_dict['name']


def create_vim_account(mydb, nfvo_tenant, datacenter_id, name=None, vim_id=None, vim_tenant=None, vim_tenant_name=None,
                       vim_username=None, vim_password=None, config=None):
    # get datacenter info
    try:
        if not datacenter_id:
            if not vim_id:
                raise NfvoException("You must provide 'vim_id", http_code=httperrors.Bad_Request)
            datacenter_id = vim_id
        datacenter_id, datacenter_name = get_datacenter_uuid(mydb, None, datacenter_id)

        create_vim_tenant = True if not vim_tenant and not vim_tenant_name else False

        # get nfvo_tenant info
        tenant_dict = mydb.get_table_by_uuid_name('nfvo_tenants', nfvo_tenant)
        if vim_tenant_name==None:
            vim_tenant_name=tenant_dict['name']

        tenants_datacenter_dict={"nfvo_tenant_id":tenant_dict['uuid'], "datacenter_id":datacenter_id }
        # #check that this association does not exist before
        # tenants_datacenters = mydb.get_rows(FROM='tenants_datacenters', WHERE=tenants_datacenter_dict)
        # if len(tenants_datacenters)>0:
        #     raise NfvoException("datacenter '{}' and tenant'{}' are already attached".format(datacenter_id, tenant_dict['uuid']), httperrors.Conflict)

        vim_tenant_id_exist_atdb=False
        if not create_vim_tenant:
            where_={"datacenter_id": datacenter_id}
            if vim_tenant!=None:
                where_["vim_tenant_id"] = vim_tenant
            if vim_tenant_name!=None:
                where_["vim_tenant_name"] = vim_tenant_name
            #check if vim_tenant_id is already at database
            datacenter_tenants_dict = mydb.get_rows(FROM='datacenter_tenants', WHERE=where_)
            if len(datacenter_tenants_dict)>=1:
                datacenter_tenants_dict = datacenter_tenants_dict[0]
                vim_tenant_id_exist_atdb=True
                #TODO check if a field has changed and edit entry at datacenter_tenants at DB
            else: #result=0
                datacenter_tenants_dict = {}
                #insert at table datacenter_tenants
        else: #if vim_tenant==None:
            #create tenant at VIM if not provided
            try:
                _, myvim = get_datacenter_by_name_uuid(mydb, None, datacenter, vim_user=vim_username,
                                                                   vim_passwd=vim_password)
                datacenter_name = myvim["name"]
                vim_tenant = myvim.new_tenant(vim_tenant_name, "created by openmano for datacenter "+datacenter_name)
            except vimconn.vimconnException as e:
                raise NfvoException("Not possible to create vim_tenant {} at VIM: {}".format(vim_tenant_id, str(e)), httperrors.Internal_Server_Error)
            datacenter_tenants_dict = {}
            datacenter_tenants_dict["created"]="true"

        #fill datacenter_tenants table
        if not vim_tenant_id_exist_atdb:
            datacenter_tenants_dict["vim_tenant_id"] = vim_tenant
            datacenter_tenants_dict["vim_tenant_name"] = vim_tenant_name
            datacenter_tenants_dict["user"] = vim_username
            datacenter_tenants_dict["passwd"] = vim_password
            datacenter_tenants_dict["datacenter_id"] = datacenter_id
            if name:
                datacenter_tenants_dict["name"] = name
            else:
                datacenter_tenants_dict["name"] = datacenter_name
            if config:
                datacenter_tenants_dict["config"] = yaml.safe_dump(config, default_flow_style=True, width=256)
            id_ = mydb.new_row('datacenter_tenants', datacenter_tenants_dict, add_uuid=True, confidential_data=True)
            datacenter_tenants_dict["uuid"] = id_

        #fill tenants_datacenters table
        datacenter_tenant_id = datacenter_tenants_dict["uuid"]
        tenants_datacenter_dict["datacenter_tenant_id"] = datacenter_tenant_id
        mydb.new_row('tenants_datacenters', tenants_datacenter_dict)

        # create thread
        thread_name = get_non_used_vim_name(datacenter_name, datacenter_id, tenant_dict['name'], tenant_dict['uuid'])
        new_thread = vim_thread.vim_thread(task_lock, thread_name, datacenter_name, datacenter_tenant_id,
                                           db=db, db_lock=db_lock, ovim=ovim)
        new_thread.start()
        thread_id = datacenter_tenants_dict["uuid"]
        vim_threads["running"][thread_id] = new_thread
        return thread_id
    except vimconn.vimconnException as e:
        raise NfvoException(str(e), httperrors.Bad_Request)


def edit_vim_account(mydb, nfvo_tenant, datacenter_tenant_id, datacenter_id=None, name=None, vim_tenant=None,
                              vim_tenant_name=None, vim_username=None, vim_password=None, config=None):

    # get vim_account; check is valid for this tenant
    from_ = "datacenter_tenants as dt JOIN tenants_datacenters as td ON dt.uuid=td.datacenter_tenant_id"
    where_ = {"td.nfvo_tenant_id": nfvo_tenant}
    if datacenter_tenant_id:
        where_["dt.uuid"] = datacenter_tenant_id
    if datacenter_id:
        where_["dt.datacenter_id"] = datacenter_id
    vim_accounts = mydb.get_rows(SELECT="dt.uuid as uuid, config", FROM=from_, WHERE=where_)
    if not vim_accounts:
        raise NfvoException("vim_account not found for this tenant", http_code=httperrors.Not_Found)
    elif len(vim_accounts) > 1:
        raise NfvoException("found more than one vim_account for this tenant", http_code=httperrors.Conflict)
    datacenter_tenant_id = vim_accounts[0]["uuid"]
    original_config = vim_accounts[0]["config"]

    update_ = {}
    if config:
        original_config_dict = yaml.load(original_config)
        original_config_dict.update(config)
        update["config"] = yaml.safe_dump(original_config_dict, default_flow_style=True, width=256)
    if name:
        update_['name'] = name
    if vim_tenant:
        update_['vim_tenant_id'] = vim_tenant
    if vim_tenant_name:
        update_['vim_tenant_name'] = vim_tenant_name
    if vim_username:
        update_['user'] = vim_username
    if vim_password:
        update_['passwd'] = vim_password
    if update_:
        mydb.update_rows("datacenter_tenants", UPDATE=update_, WHERE={"uuid": datacenter_tenant_id})

    vim_threads["running"][datacenter_tenant_id].insert_task("reload")
    return datacenter_tenant_id

def delete_vim_account(mydb, tenant_id, vim_account_id, datacenter=None):
    #get nfvo_tenant info
    if not tenant_id or tenant_id=="any":
        tenant_uuid = None
    else:
        tenant_dict = mydb.get_table_by_uuid_name('nfvo_tenants', tenant_id)
        tenant_uuid = tenant_dict['uuid']

    #check that this association exist before
    tenants_datacenter_dict = {}
    if datacenter:
        datacenter_id, _ = get_datacenter_uuid(mydb, tenant_uuid, datacenter)
        tenants_datacenter_dict["datacenter_id"] = datacenter_id
    elif vim_account_id:
        tenants_datacenter_dict["datacenter_tenant_id"] = vim_account_id
    if tenant_uuid:
        tenants_datacenter_dict["nfvo_tenant_id"] = tenant_uuid
    tenant_datacenter_list = mydb.get_rows(FROM='tenants_datacenters', WHERE=tenants_datacenter_dict)
    if len(tenant_datacenter_list)==0 and tenant_uuid:
        raise NfvoException("datacenter '{}' and tenant '{}' are not attached".format(datacenter_id, tenant_dict['uuid']), httperrors.Not_Found)

    #delete this association
    mydb.delete_row(FROM='tenants_datacenters', WHERE=tenants_datacenter_dict)

    #get vim_tenant info and deletes
    warning=''
    for tenant_datacenter_item in tenant_datacenter_list:
        vim_tenant_dict = mydb.get_table_by_uuid_name('datacenter_tenants', tenant_datacenter_item['datacenter_tenant_id'])
        #try to delete vim:tenant
        try:
            mydb.delete_row_by_id('datacenter_tenants', tenant_datacenter_item['datacenter_tenant_id'])
            if vim_tenant_dict['created']=='true':
                #delete tenant at VIM if created by NFVO
                try:
                    datacenter_id, myvim = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)
                    myvim.delete_tenant(vim_tenant_dict['vim_tenant_id'])
                except vimconn.vimconnException as e:
                    warning = "Not possible to delete vim_tenant_id {} from VIM: {} ".format(vim_tenant_dict['vim_tenant_id'], str(e))
                    logger.warn(warning)
        except db_base_Exception as e:
            logger.error("Cannot delete datacenter_tenants " + str(e))
            pass  # the error will be caused because dependencies, vim_tenant can not be deleted
        thread_id = tenant_datacenter_item["datacenter_tenant_id"]
        thread = vim_threads["running"].get(thread_id)
        if thread:
            thread.insert_task("exit")
            vim_threads["deleting"][thread_id] = thread
    return "datacenter {} detached. {}".format(datacenter_id, warning)


def datacenter_action(mydb, tenant_id, datacenter, action_dict):
    #DEPRECATED
    #get datacenter info
    datacenter_id, myvim  = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)

    if 'net-update' in action_dict:
        try:
            nets = myvim.get_network_list(filter_dict={'shared': True, 'admin_state_up': True, 'status': 'ACTIVE'})
            #print content
        except vimconn.vimconnException as e:
            #logger.error("nfvo.datacenter_action() Not possible to get_network_list from VIM: %s ", str(e))
            raise NfvoException(str(e), httperrors.Internal_Server_Error)
        #update nets Change from VIM format to NFVO format
        net_list=[]
        for net in nets:
            net_nfvo={'datacenter_id': datacenter_id}
            net_nfvo['name']       = net['name']
            #net_nfvo['description']= net['name']
            net_nfvo['vim_net_id'] = net['id']
            net_nfvo['type']       = net['type'][0:6] #change from ('ptp','data','bridge_data','bridge_man')  to ('bridge','data','ptp')
            net_nfvo['shared']     = net['shared']
            net_nfvo['multipoint'] = False if net['type']=='ptp' else True
            net_list.append(net_nfvo)
        inserted, deleted = mydb.update_datacenter_nets(datacenter_id, net_list)
        logger.info("Inserted %d nets, deleted %d old nets", inserted, deleted)
        return inserted
    elif 'net-edit' in action_dict:
        net = action_dict['net-edit'].pop('net')
        what = 'vim_net_id' if utils.check_valid_uuid(net) else 'name'
        result = mydb.update_rows('datacenter_nets', action_dict['net-edit'],
                                WHERE={'datacenter_id':datacenter_id, what: net})
        return result
    elif 'net-delete' in action_dict:
        net = action_dict['net-deelte'].get('net')
        what = 'vim_net_id' if utils.check_valid_uuid(net) else 'name'
        result = mydb.delete_row(FROM='datacenter_nets',
                                WHERE={'datacenter_id':datacenter_id, what: net})
        return result

    else:
        raise NfvoException("Unknown action " + str(action_dict), httperrors.Bad_Request)


def datacenter_edit_netmap(mydb, tenant_id, datacenter, netmap, action_dict):
    #get datacenter info
    datacenter_id, _  = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)

    what = 'uuid' if utils.check_valid_uuid(netmap) else 'name'
    result = mydb.update_rows('datacenter_nets', action_dict['netmap'],
                            WHERE={'datacenter_id':datacenter_id, what: netmap})
    return result


def datacenter_new_netmap(mydb, tenant_id, datacenter, action_dict=None):
    #get datacenter info
    datacenter_id, myvim  = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)
    filter_dict={}
    if action_dict:
        action_dict = action_dict["netmap"]
        if 'vim_id' in action_dict:
            filter_dict["id"] = action_dict['vim_id']
        if 'vim_name' in action_dict:
            filter_dict["name"] = action_dict['vim_name']
    else:
        filter_dict["shared"] = True

    try:
        vim_nets = myvim.get_network_list(filter_dict=filter_dict)
    except vimconn.vimconnException as e:
        #logger.error("nfvo.datacenter_new_netmap() Not possible to get_network_list from VIM: %s ", str(e))
        raise NfvoException(str(e), httperrors.Internal_Server_Error)
    if len(vim_nets)>1 and action_dict:
        raise NfvoException("more than two networks found, specify with vim_id", httperrors.Conflict)
    elif len(vim_nets)==0: # and action_dict:
        raise NfvoException("Not found a network at VIM with " + str(filter_dict), httperrors.Not_Found)
    net_list=[]
    for net in vim_nets:
        net_nfvo={'datacenter_id': datacenter_id}
        if action_dict and "name" in action_dict:
            net_nfvo['name']       = action_dict['name']
        else:
            net_nfvo['name']       = net['name']
        #net_nfvo['description']= net['name']
        net_nfvo['vim_net_id'] = net['id']
        net_nfvo['type']       = net['type'][0:6] #change from ('ptp','data','bridge_data','bridge_man')  to ('bridge','data','ptp')
        net_nfvo['shared']     = net['shared']
        net_nfvo['multipoint'] = False if net['type']=='ptp' else True
        try:
            net_id = mydb.new_row("datacenter_nets", net_nfvo, add_uuid=True)
            net_nfvo["status"] = "OK"
            net_nfvo["uuid"] = net_id
        except db_base_Exception as e:
            if action_dict:
                raise
            else:
                net_nfvo["status"] = "FAIL: " + str(e)
        net_list.append(net_nfvo)
    return net_list

def get_sdn_net_id(mydb, tenant_id, datacenter, network_id):
    # obtain all network data
    try:
        if utils.check_valid_uuid(network_id):
            filter_dict = {"id": network_id}
        else:
            filter_dict = {"name": network_id}

        datacenter_id, myvim = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)
        network = myvim.get_network_list(filter_dict=filter_dict)
    except vimconn.vimconnException as e:
        raise NfvoException("Not possible to get_sdn_net_id from VIM: {}".format(str(e)), e.http_code)

    # ensure the network is defined
    if len(network) == 0:
        raise NfvoException("Network {} is not present in the system".format(network_id),
                            httperrors.Bad_Request)

    # ensure there is only one network with the provided name
    if len(network) > 1:
        raise NfvoException("Multiple networks present in vim identified by {}".format(network_id), httperrors.Bad_Request)

    # ensure it is a dataplane network
    if network[0]['type'] != 'data':
        return None

    # ensure we use the id
    network_id = network[0]['id']

    # search in dabase mano_db in table instance nets for the sdn_net_id that corresponds to the vim_net_id==network_id
    # and with instance_scenario_id==NULL
    #search_dict = {'vim_net_id': network_id, 'instance_scenario_id': None}
    search_dict = {'vim_net_id': network_id}

    try:
        #sdn_network_id = mydb.get_rows(SELECT=('sdn_net_id',), FROM='instance_nets', WHERE=search_dict)[0]['sdn_net_id']
        result =  mydb.get_rows(SELECT=('sdn_net_id',), FROM='instance_nets', WHERE=search_dict)
    except db_base_Exception as e:
        raise NfvoException("db_base_Exception obtaining SDN network to associated to vim network {}".format(
            network_id) + str(e), e.http_code)

    sdn_net_counter = 0
    for net in result:
        if net['sdn_net_id'] != None:
            sdn_net_counter+=1
            sdn_net_id = net['sdn_net_id']

    if sdn_net_counter == 0:
        return None
    elif sdn_net_counter == 1:
        return sdn_net_id
    else:
        raise NfvoException("More than one SDN network is associated to vim network {}".format(
            network_id), httperrors.Internal_Server_Error)

def get_sdn_controller_id(mydb, datacenter):
    # Obtain sdn controller id
    config = mydb.get_rows(SELECT=('config',), FROM='datacenters', WHERE={'uuid': datacenter})[0].get('config', '{}')
    if not config:
        return None

    return yaml.load(config).get('sdn-controller')

def vim_net_sdn_attach(mydb, tenant_id, datacenter, network_id, descriptor):
    try:
        sdn_network_id = get_sdn_net_id(mydb, tenant_id, datacenter, network_id)
        if not sdn_network_id:
            raise NfvoException("No SDN network is associated to vim-network {}".format(network_id), httperrors.Internal_Server_Error)

        #Obtain sdn controller id
        controller_id = get_sdn_controller_id(mydb, datacenter)
        if not controller_id:
            raise NfvoException("No SDN controller is set for datacenter {}".format(datacenter), httperrors.Internal_Server_Error)

        #Obtain sdn controller info
        sdn_controller = ovim.show_of_controller(controller_id)

        port_data = {
            'name': 'external_port',
            'net_id': sdn_network_id,
            'ofc_id': controller_id,
            'switch_dpid': sdn_controller['dpid'],
            'switch_port': descriptor['port']
        }

        if 'vlan' in descriptor:
            port_data['vlan'] = descriptor['vlan']
        if 'mac' in descriptor:
            port_data['mac'] = descriptor['mac']

        result = ovim.new_port(port_data)
    except ovimException as e:
        raise NfvoException("ovimException attaching SDN network {} to vim network {}".format(
            sdn_network_id, network_id) + str(e), httperrors.Internal_Server_Error)
    except db_base_Exception as e:
        raise NfvoException("db_base_Exception attaching SDN network to vim network {}".format(
            network_id) + str(e), e.http_code)

    return 'Port uuid: '+ result

def vim_net_sdn_detach(mydb, tenant_id, datacenter, network_id, port_id=None):
    if port_id:
        filter = {'uuid': port_id}
    else:
        sdn_network_id = get_sdn_net_id(mydb, tenant_id, datacenter, network_id)
        if not sdn_network_id:
            raise NfvoException("No SDN network is associated to vim-network {}".format(network_id),
                                httperrors.Internal_Server_Error)
        #in case no port_id is specified only ports marked as 'external_port' will be detached
        filter = {'name': 'external_port', 'net_id': sdn_network_id}

    try:
        port_list = ovim.get_ports(columns={'uuid'}, filter=filter)
    except ovimException as e:
        raise NfvoException("ovimException obtaining external ports for net {}. ".format(network_id) + str(e),
                            httperrors.Internal_Server_Error)

    if len(port_list) == 0:
        raise NfvoException("No ports attached to the network {} were found with the requested criteria".format(network_id),
                            httperrors.Bad_Request)

    port_uuid_list = []
    for port in port_list:
        try:
            port_uuid_list.append(port['uuid'])
            ovim.delete_port(port['uuid'])
        except ovimException as e:
            raise NfvoException("ovimException deleting port {} for net {}. ".format(port['uuid'], network_id) + str(e), httperrors.Internal_Server_Error)

    return 'Detached ports uuid: {}'.format(','.join(port_uuid_list))

def vim_action_get(mydb, tenant_id, datacenter, item, name):
    #get datacenter info
    datacenter_id, myvim  = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)
    filter_dict={}
    if name:
        if utils.check_valid_uuid(name):
            filter_dict["id"] = name
        else:
            filter_dict["name"] = name
    try:
        if item=="networks":
            #filter_dict['tenant_id'] = myvim['tenant_id']
            content = myvim.get_network_list(filter_dict=filter_dict)

            if len(content) == 0:
                raise NfvoException("Network {} is not present in the system. ".format(name),
                                    httperrors.Bad_Request)

            #Update the networks with the attached ports
            for net in content:
                sdn_network_id = get_sdn_net_id(mydb, tenant_id, datacenter, net['id'])
                if sdn_network_id != None:
                    try:
                        #port_list = ovim.get_ports(columns={'uuid', 'switch_port', 'vlan'}, filter={'name': 'external_port', 'net_id': sdn_network_id})
                        port_list = ovim.get_ports(columns={'uuid', 'switch_port', 'vlan','name'}, filter={'net_id': sdn_network_id})
                    except ovimException as e:
                        raise NfvoException("ovimException obtaining external ports for net {}. ".format(network_id) + str(e), httperrors.Internal_Server_Error)
                    #Remove field name and if port name is external_port save it as 'type'
                    for port in port_list:
                        if port['name'] == 'external_port':
                            port['type'] = "External"
                        del port['name']
                    net['sdn_network_id'] = sdn_network_id
                    net['sdn_attached_ports'] = port_list

        elif item=="tenants":
            content = myvim.get_tenant_list(filter_dict=filter_dict)
        elif item == "images":

            content = myvim.get_image_list(filter_dict=filter_dict)
        else:
            raise NfvoException(item + "?", httperrors.Method_Not_Allowed)
        logger.debug("vim_action response %s", content) #update nets Change from VIM format to NFVO format
        if name and len(content)==1:
            return {item[:-1]: content[0]}
        elif name and len(content)==0:
            raise NfvoException("No {} found with ".format(item[:-1]) + " and ".join(map(lambda x: str(x[0])+": "+str(x[1]), filter_dict.iteritems())),
                 datacenter)
        else:
            return {item: content}
    except vimconn.vimconnException as e:
        print "vim_action Not possible to get_%s_list from VIM: %s " % (item, str(e))
        raise NfvoException("Not possible to get_{}_list from VIM: {}".format(item, str(e)), e.http_code)


def vim_action_delete(mydb, tenant_id, datacenter, item, name):
    #get datacenter info
    if tenant_id == "any":
        tenant_id=None

    datacenter_id, myvim  = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)
    #get uuid name
    content = vim_action_get(mydb, tenant_id, datacenter, item, name)
    logger.debug("vim_action_delete vim response: " + str(content))
    items = content.values()[0]
    if type(items)==list and len(items)==0:
        raise NfvoException("Not found " + item, httperrors.Not_Found)
    elif type(items)==list and len(items)>1:
        raise NfvoException("Found more than one {} with this name. Use uuid.".format(item), httperrors.Not_Found)
    else: # it is a dict
        item_id = items["id"]
        item_name = str(items.get("name"))

    try:
        if item=="networks":
            # If there is a SDN network associated to the vim-network, proceed to clear the relationship and delete it
            sdn_network_id = get_sdn_net_id(mydb, tenant_id, datacenter, item_id)
            if sdn_network_id != None:
                #Delete any port attachment to this network
                try:
                    port_list = ovim.get_ports(columns={'uuid'}, filter={'net_id': sdn_network_id})
                except ovimException as e:
                    raise NfvoException(
                        "ovimException obtaining external ports for net {}. ".format(network_id) + str(e),
                        httperrors.Internal_Server_Error)

                # By calling one by one all ports to be detached we ensure that not only the external_ports get detached
                for port in port_list:
                    vim_net_sdn_detach(mydb, tenant_id, datacenter, item_id, port['uuid'])

                #Delete from 'instance_nets' the correspondence between the vim-net-id and the sdn-net-id
                try:
                    mydb.delete_row(FROM='instance_nets', WHERE={'instance_scenario_id': None, 'sdn_net_id': sdn_network_id, 'vim_net_id': item_id})
                except db_base_Exception as e:
                    raise NfvoException("Error deleting correspondence for VIM/SDN dataplane networks{}: ".format(correspondence) +
                                        str(e), e.http_code)

                #Delete the SDN network
                try:
                    ovim.delete_network(sdn_network_id)
                except ovimException as e:
                    logger.error("ovimException deleting SDN network={} ".format(sdn_network_id) + str(e), exc_info=True)
                    raise NfvoException("ovimException deleting SDN network={} ".format(sdn_network_id) + str(e),
                                        httperrors.Internal_Server_Error)

            content = myvim.delete_network(item_id)
        elif item=="tenants":
            content = myvim.delete_tenant(item_id)
        elif item == "images":
            content = myvim.delete_image(item_id)
        else:
            raise NfvoException(item + "?", httperrors.Method_Not_Allowed)
    except vimconn.vimconnException as e:
        #logger.error( "vim_action Not possible to delete_{} {}from VIM: {} ".format(item, name, str(e)))
        raise NfvoException("Not possible to delete_{} {} from VIM: {}".format(item, name, str(e)), e.http_code)

    return "{} {} {} deleted".format(item[:-1], item_id,item_name)


def vim_action_create(mydb, tenant_id, datacenter, item, descriptor):
    #get datacenter info
    logger.debug("vim_action_create descriptor %s", str(descriptor))
    if tenant_id == "any":
        tenant_id=None
    datacenter_id, myvim  = get_datacenter_by_name_uuid(mydb, tenant_id, datacenter)
    try:
        if item=="networks":
            net = descriptor["network"]
            net_name = net.pop("name")
            net_type = net.pop("type", "bridge")
            net_public = net.pop("shared", False)
            net_ipprofile = net.pop("ip_profile", None)
            net_vlan = net.pop("vlan", None)
            content, _ = myvim.new_network(net_name, net_type, net_ipprofile, shared=net_public, vlan=net_vlan) #, **net)

            #If the datacenter has a SDN controller defined and the network is of dataplane type, then create the sdn network
            if get_sdn_controller_id(mydb, datacenter) != None and (net_type == 'data' or net_type == 'ptp'):
                #obtain datacenter_tenant_id
                datacenter_tenant_id = mydb.get_rows(SELECT=('uuid',),
                                                     FROM='datacenter_tenants',
                                                     WHERE={'datacenter_id': datacenter})[0]['uuid']
                try:
                    sdn_network = {}
                    sdn_network['vlan'] = net_vlan
                    sdn_network['type'] = net_type
                    sdn_network['name'] = net_name
                    sdn_network['region'] = datacenter_tenant_id
                    ovim_content  = ovim.new_network(sdn_network)
                except ovimException as e:
                    logger.error("ovimException creating SDN network={} ".format(
                        sdn_network) + str(e), exc_info=True)
                    raise NfvoException("ovimException creating SDN network={} ".format(sdn_network) + str(e),
                                        httperrors.Internal_Server_Error)

                # Save entry in in dabase mano_db in table instance_nets to stablish a dictionary  vim_net_id <->sdn_net_id
                # use instance_scenario_id=None to distinguish from real instaces of nets
                correspondence = {'instance_scenario_id': None,
                                  'sdn_net_id': ovim_content,
                                  'vim_net_id': content,
                                  'datacenter_tenant_id': datacenter_tenant_id
                                  }
                try:
                    mydb.new_row('instance_nets', correspondence, add_uuid=True)
                except db_base_Exception as e:
                    raise NfvoException("Error saving correspondence for VIM/SDN dataplane networks{}: {}".format(
                        correspondence, e), e.http_code)
        elif item=="tenants":
            tenant = descriptor["tenant"]
            content = myvim.new_tenant(tenant["name"], tenant.get("description"))
        else:
            raise NfvoException(item + "?", httperrors.Method_Not_Allowed)
    except vimconn.vimconnException as e:
        raise NfvoException("Not possible to create {} at VIM: {}".format(item, str(e)), e.http_code)

    return vim_action_get(mydb, tenant_id, datacenter, item, content)

def sdn_controller_create(mydb, tenant_id, sdn_controller):
    data = ovim.new_of_controller(sdn_controller)
    logger.debug('New SDN controller created with uuid {}'.format(data))
    return data

def sdn_controller_update(mydb, tenant_id, controller_id, sdn_controller):
    data = ovim.edit_of_controller(controller_id, sdn_controller)
    msg = 'SDN controller {} updated'.format(data)
    logger.debug(msg)
    return msg

def sdn_controller_list(mydb, tenant_id, controller_id=None):
    if controller_id == None:
        data = ovim.get_of_controllers()
    else:
        data = ovim.show_of_controller(controller_id)

    msg = 'SDN controller list:\n {}'.format(data)
    logger.debug(msg)
    return data

def sdn_controller_delete(mydb, tenant_id, controller_id):
    select_ = ('uuid', 'config')
    datacenters = mydb.get_rows(FROM='datacenters', SELECT=select_)
    for datacenter in datacenters:
        if datacenter['config']:
            config = yaml.load(datacenter['config'])
            if 'sdn-controller' in config and config['sdn-controller'] == controller_id:
                raise NfvoException("SDN controller {} is in use by datacenter {}".format(controller_id, datacenter['uuid']), httperrors.Conflict)

    data = ovim.delete_of_controller(controller_id)
    msg = 'SDN controller {} deleted'.format(data)
    logger.debug(msg)
    return msg

def datacenter_sdn_port_mapping_set(mydb, tenant_id, datacenter_id, sdn_port_mapping):
    controller = mydb.get_rows(FROM="datacenters", SELECT=("config",), WHERE={"uuid":datacenter_id})
    if len(controller) < 1:
        raise NfvoException("Datacenter {} not present in the database".format(datacenter_id), httperrors.Not_Found)

    try:
        sdn_controller_id = yaml.load(controller[0]["config"])["sdn-controller"]
    except:
        raise NfvoException("The datacenter {} has not an SDN controller associated".format(datacenter_id), httperrors.Bad_Request)

    sdn_controller = ovim.show_of_controller(sdn_controller_id)
    switch_dpid = sdn_controller["dpid"]

    maps = list()
    for compute_node in sdn_port_mapping:
        #element = {"ofc_id": sdn_controller_id, "region": datacenter_id, "switch_dpid": switch_dpid}
        element = dict()
        element["compute_node"] = compute_node["compute_node"]
        for port in compute_node["ports"]:
            pci = port.get("pci")
            element["switch_port"] = port.get("switch_port")
            element["switch_mac"] = port.get("switch_mac")
            if not element["switch_port"] and not element["switch_mac"]:
                raise NfvoException ("The mapping must contain 'switch_port' or 'switch_mac'", httperrors.Bad_Request)
            for pci_expanded in utils.expand_brackets(pci):
                element["pci"] = pci_expanded
                maps.append(dict(element))

    return ovim.set_of_port_mapping(maps, ofc_id=sdn_controller_id, switch_dpid=switch_dpid, region=datacenter_id)

def datacenter_sdn_port_mapping_list(mydb, tenant_id, datacenter_id):
    maps = ovim.get_of_port_mappings(db_filter={"region": datacenter_id})

    result = {
        "sdn-controller": None,
        "datacenter-id": datacenter_id,
        "dpid": None,
        "ports_mapping": list()
    }

    datacenter = mydb.get_table_by_uuid_name('datacenters', datacenter_id)
    if datacenter['config']:
        config = yaml.load(datacenter['config'])
        if 'sdn-controller' in config:
            controller_id = config['sdn-controller']
            sdn_controller = sdn_controller_list(mydb, tenant_id, controller_id)
            result["sdn-controller"] = controller_id
            result["dpid"] = sdn_controller["dpid"]

    if result["sdn-controller"] == None:
        raise NfvoException("SDN controller is not defined for datacenter {}".format(datacenter_id), httperrors.Bad_Request)
    if result["dpid"] == None:
        raise NfvoException("It was not possible to determine DPID for SDN controller {}".format(result["sdn-controller"]),
                        httperrors.Internal_Server_Error)

    if len(maps) == 0:
        return result

    ports_correspondence_dict = dict()
    for link in maps:
        if result["sdn-controller"] != link["ofc_id"]:
            raise NfvoException("The sdn-controller specified for different port mappings differ", httperrors.Internal_Server_Error)
        if result["dpid"] != link["switch_dpid"]:
            raise NfvoException("The dpid specified for different port mappings differ", httperrors.Internal_Server_Error)
        element = dict()
        element["pci"] = link["pci"]
        if link["switch_port"]:
            element["switch_port"] = link["switch_port"]
        if link["switch_mac"]:
            element["switch_mac"] = link["switch_mac"]

        if not link["compute_node"] in ports_correspondence_dict:
            content = dict()
            content["compute_node"] = link["compute_node"]
            content["ports"] = list()
            ports_correspondence_dict[link["compute_node"]] = content

        ports_correspondence_dict[link["compute_node"]]["ports"].append(element)

    for key in sorted(ports_correspondence_dict):
        result["ports_mapping"].append(ports_correspondence_dict[key])

    return result

def datacenter_sdn_port_mapping_delete(mydb, tenant_id, datacenter_id):
    return ovim.clear_of_port_mapping(db_filter={"region":datacenter_id})

def create_RO_keypair(tenant_id):
    """
    Creates a public / private keys for a RO tenant and returns their values
    Params:
        tenant_id: ID of the tenant
    Return:
        public_key: Public key for the RO tenant
        private_key: Encrypted private key for RO tenant
    """

    bits = 2048
    key = RSA.generate(bits)
    try:
        public_key = key.publickey().exportKey('OpenSSH')
        if isinstance(public_key, ValueError):
            raise NfvoException("Unable to create public key: {}".format(public_key), httperrors.Internal_Server_Error)
        private_key = key.exportKey(passphrase=tenant_id, pkcs=8)
    except (ValueError, NameError) as e:
        raise NfvoException("Unable to create private key: {}".format(e), httperrors.Internal_Server_Error)
    return public_key, private_key

def decrypt_key (key, tenant_id):
    """
    Decrypts an encrypted RSA key
    Params:
        key: Private key to be decrypted
        tenant_id: ID of the tenant
    Return:
        unencrypted_key: Unencrypted private key for RO tenant
    """
    try:
        key = RSA.importKey(key,tenant_id)
        unencrypted_key = key.exportKey('PEM')
        if isinstance(unencrypted_key, ValueError):
            raise NfvoException("Unable to decrypt the private key: {}".format(unencrypted_key), httperrors.Internal_Server_Error)
    except ValueError as e:
        raise NfvoException("Unable to decrypt the private key: {}".format(e), httperrors.Internal_Server_Error)
    return unencrypted_key
