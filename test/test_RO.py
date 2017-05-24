#!/usr/bin/env python2
# -*- coding: utf-8 -*-

##
# Copyright 2017
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
##

'''
Module for testing openmano functionality. It uses openmanoclient.py for invoking openmano
'''
__author__="Pablo Montes, Alfonso Tierno"
__date__ ="$16-Feb-2017 17:08:16$"
__version__="0.0.3"
version_date="May 2017"

import logging
import os
from argparse import ArgumentParser
import argcomplete
import unittest
import string
import inspect
import random
import traceback
import glob
import yaml
import sys
import time

global test_config   #  used for global variables with the test configuration
test_config = {}


def check_instance_scenario_active(uuid):
    instance = test_config["client"].get_instance(uuid=uuid)

    for net in instance['nets']:
        status = net['status']
        if status != 'ACTIVE':
            return (False, status)

    for vnf in instance['vnfs']:
        for vm in vnf['vms']:
            status = vm['status']
            if status != 'ACTIVE':
                return (False, status)

    return (True, None)


'''
IMPORTANT NOTE
All unittest classes for code based tests must have prefix 'test_' in order to be taken into account for tests
'''
class test_VIM_datacenter_tenant_operations(unittest.TestCase):
    test_index = 1
    tenant_name = None
    test_text = None

    @classmethod
    def setUpClass(cls):
        logger.info("{}. {}".format(test_config["test_number"], cls.__name__))

    @classmethod
    def tearDownClass(cls):
        test_config["test_number"] += 1

    def tearDown(self):
        exec_info = sys.exc_info()
        if exec_info == (None, None, None):
            logger.info(self.__class__.test_text+" -> TEST OK")
        else:
            logger.warning(self.__class__.test_text+" -> TEST NOK")
            error_trace = traceback.format_exception(exec_info[0], exec_info[1], exec_info[2])
            msg = ""
            for line in error_trace:
                msg = msg + line
            logger.critical("{}".format(msg))

    def test_000_create_RO_tenant(self):
        self.__class__.tenant_name = _get_random_string(20)
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.test_index += 1
        tenant = test_config["client"].create_tenant(name=self.__class__.tenant_name,
                                                     description=self.__class__.tenant_name)
        logger.debug("{}".format(tenant))
        self.assertEqual(tenant.get('tenant', {}).get('name', ''), self.__class__.tenant_name)

    def test_010_list_RO_tenant(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.test_index += 1
        tenant = test_config["client"].get_tenant(name=self.__class__.tenant_name)
        logger.debug("{}".format(tenant))
        self.assertEqual(tenant.get('tenant', {}).get('name', ''), self.__class__.tenant_name)

    def test_020_delete_RO_tenant(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.test_index += 1
        tenant = test_config["client"].delete_tenant(name=self.__class__.tenant_name)
        logger.debug("{}".format(tenant))
        assert('deleted' in tenant.get('result',""))


class test_VIM_datacenter_operations(unittest.TestCase):
    test_index = 1
    datacenter_name = None
    test_text = None

    @classmethod
    def setUpClass(cls):
        logger.info("{}. {}".format(test_config["test_number"], cls.__name__))

    @classmethod
    def tearDownClass(cls):
        test_config["test_number"] += 1

    def tearDown(self):
        exec_info = sys.exc_info()
        if exec_info == (None, None, None):
            logger.info(self.__class__.test_text+" -> TEST OK")
        else:
            logger.warning(self.__class__.test_text+" -> TEST NOK")
            error_trace = traceback.format_exception(exec_info[0], exec_info[1], exec_info[2])
            msg = ""
            for line in error_trace:
                msg = msg + line
            logger.critical("{}".format(msg))

    def test_000_create_datacenter(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.datacenter_name = _get_random_string(20)
        self.__class__.test_index += 1
        self.datacenter = test_config["client"].create_datacenter(name=self.__class__.datacenter_name,
                                                                  vim_url="http://fakeurl/fake")
        logger.debug("{}".format(self.datacenter))
        self.assertEqual (self.datacenter.get('datacenter', {}).get('name',''), self.__class__.datacenter_name)

    def test_010_list_datacenter(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        self.datacenter = test_config["client"].get_datacenter(all_tenants=True, name=self.__class__.datacenter_name)
        logger.debug("{}".format(self.datacenter))
        self.assertEqual (self.datacenter.get('datacenter', {}).get('name', ''), self.__class__.datacenter_name)

    def test_020_attach_datacenter(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        self.datacenter = test_config["client"].attach_datacenter(name=self.__class__.datacenter_name,
                                                                  vim_tenant_name='fake')
        logger.debug("{}".format(self.datacenter))
        assert ('vim_tenants' in self.datacenter.get('datacenter', {}))

    def test_030_list_attached_datacenter(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        self.datacenter = test_config["client"].get_datacenter(all_tenants=False, name=self.__class__.datacenter_name)
        logger.debug("{}".format(self.datacenter))
        self.assertEqual (self.datacenter.get('datacenter', {}).get('name', ''), self.__class__.datacenter_name)

    def test_040_detach_datacenter(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        self.datacenter = test_config["client"].detach_datacenter(name=self.__class__.datacenter_name)
        logger.debug("{}".format(self.datacenter))
        assert ('detached' in self.datacenter.get('result', ""))

    def test_050_delete_datacenter(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        self.datacenter = test_config["client"].delete_datacenter(name=self.__class__.datacenter_name)
        logger.debug("{}".format(self.datacenter))
        assert('deleted' in self.datacenter.get('result',""))


class test_VIM_network_operations(unittest.TestCase):
    test_index = 1
    vim_network_name = None
    test_text = None
    vim_network_uuid = None

    @classmethod
    def setUpClass(cls):
        logger.info("{}. {}".format(test_config["test_number"], cls.__name__))

    @classmethod
    def tearDownClass(cls):
        test_config["test_number"] += 1

    def tearDown(self):
        exec_info = sys.exc_info()
        if exec_info == (None, None, None):
            logger.info(self.__class__.test_text + " -> TEST OK")
        else:
            logger.warning(self.__class__.test_text + " -> TEST NOK")
            error_trace = traceback.format_exception(exec_info[0], exec_info[1], exec_info[2])
            msg = ""
            for line in error_trace:
                msg = msg + line
            logger.critical("{}".format(msg))

    def test_000_create_VIM_network(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.vim_network_name = _get_random_string(20)
        self.__class__.test_index += 1
        network = test_config["client"].vim_action("create", "networks", name=self.__class__.vim_network_name)
        logger.debug("{}".format(network))
        self.__class__.vim_network_uuid = network["network"]["id"]
        self.assertEqual(network.get('network', {}).get('name', ''), self.__class__.vim_network_name)

    def test_010_list_VIM_networks(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.test_index += 1
        networks = test_config["client"].vim_action("list", "networks")
        logger.debug("{}".format(networks))

    def test_020_get_VIM_network_by_uuid(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        network = test_config["client"].vim_action("show", "networks", uuid=self.__class__.vim_network_uuid)
        logger.debug("{}".format(network))
        self.assertEqual(network.get('network', {}).get('name', ''), self.__class__.vim_network_name)

    def test_030_delete_VIM_network_by_uuid(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        network = test_config["client"].vim_action("delete", "networks", uuid=self.__class__.vim_network_uuid)
        logger.debug("{}".format(network))
        assert ('deleted' in network.get('result', ""))


class test_VIM_image_operations(unittest.TestCase):
    test_index = 1
    test_text = None

    @classmethod
    def setUpClass(cls):
        logger.info("{}. {}".format(test_config["test_number"], cls.__name__))

    @classmethod
    def tearDownClass(cls):
        test_config["test_number"] += 1

    def tearDown(self):
        exec_info = sys.exc_info()
        if exec_info == (None, None, None):
            logger.info(self.__class__.test_text + " -> TEST OK")
        else:
            logger.warning(self.__class__.test_text + " -> TEST NOK")
            error_trace = traceback.format_exception(exec_info[0], exec_info[1], exec_info[2])
            msg = ""
            for line in error_trace:
                msg = msg + line
            logger.critical("{}".format(msg))

    def test_000_list_VIM_images(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.test_index += 1
        images = test_config["client"].vim_action("list", "images")
        logger.debug("{}".format(images))

'''
The following is a non critical test that will fail most of the times.
In case of OpenStack datacenter these tests will only success if RO has access to the admin endpoint
This test will only be executed in case it is specifically requested by the user
'''
class test_VIM_tenant_operations(unittest.TestCase):
    test_index = 1
    vim_tenant_name = None
    test_text = None
    vim_tenant_uuid = None

    @classmethod
    def setUpClass(cls):
        logger.info("{}. {}".format(test_config["test_number"], cls.__name__))
        logger.warning("In case of OpenStack datacenter these tests will only success "
                       "if RO has access to the admin endpoint")

    @classmethod
    def tearDownClass(cls):
        test_config["test_number"] += 1

    def tearDown(self):
        exec_info = sys.exc_info()
        if exec_info == (None, None, None):
            logger.info(self.__class__.test_text + " -> TEST OK")
        else:
            logger.warning(self.__class__.test_text + " -> TEST NOK")
            error_trace = traceback.format_exception(exec_info[0], exec_info[1], exec_info[2])
            msg = ""
            for line in error_trace:
                msg = msg + line
            logger.critical("{}".format(msg))

    def test_000_create_VIM_tenant(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.vim_tenant_name = _get_random_string(20)
        self.__class__.test_index += 1
        tenant = test_config["client"].vim_action("create", "tenants", name=self.__class__.vim_tenant_name)
        logger.debug("{}".format(tenant))
        self.__class__.vim_tenant_uuid = tenant["tenant"]["id"]
        self.assertEqual(tenant.get('tenant', {}).get('name', ''), self.__class__.vim_tenant_name)

    def test_010_list_VIM_tenants(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)
        self.__class__.test_index += 1
        tenants = test_config["client"].vim_action("list", "tenants")
        logger.debug("{}".format(tenants))

    def test_020_get_VIM_tenant_by_uuid(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        tenant = test_config["client"].vim_action("show", "tenants", uuid=self.__class__.vim_tenant_uuid)
        logger.debug("{}".format(tenant))
        self.assertEqual(tenant.get('tenant', {}).get('name', ''), self.__class__.vim_tenant_name)

    def test_030_delete_VIM_tenant_by_uuid(self):
        self.__class__.test_text = "{}.{}. TEST {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name)

        self.__class__.test_index += 1
        tenant = test_config["client"].vim_action("delete", "tenants", uuid=self.__class__.vim_tenant_uuid)
        logger.debug("{}".format(tenant))
        assert ('deleted' in tenant.get('result', ""))

'''
IMPORTANT NOTE
The following unittest class does not have the 'test_' on purpose. This test is the one used for the
scenario based tests.
'''
class descriptor_based_scenario_test(unittest.TestCase):
    test_index = 0
    test_text = None
    scenario_test_path = None
    scenario_uuid = None
    instance_scenario_uuid = None
    to_delete_list = []

    @classmethod
    def setUpClass(cls):
        cls.test_index = 1
        cls.to_delete_list = []
        cls.scenario_test_path = test_config["test_directory"] + '/' + test_config["test_folder"]
        logger.info("{}. {} {}".format(test_config["test_number"], cls.__name__, test_config["test_folder"]))

    @classmethod
    def tearDownClass(cls):
         test_config["test_number"] += 1

    def tearDown(self):
        exec_info = sys.exc_info()
        if exec_info == (None, None, None):
            logger.info(self.__class__.test_text + " -> TEST OK")
        else:
            logger.warning(self.__class__.test_text + " -> TEST NOK")
            error_trace = traceback.format_exception(exec_info[0], exec_info[1], exec_info[2])
            msg = ""
            for line in error_trace:
                msg = msg + line
            logger.critical("{}".format(msg))


    def test_000_load_scenario(self):
        self.__class__.test_text = "{}.{}. TEST {} {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name,
                                                           test_config["test_folder"])
        self.__class__.test_index += 1
        vnfd_files = glob.glob(self.__class__.scenario_test_path+'/vnfd_*.yaml')
        scenario_file = glob.glob(self.__class__.scenario_test_path + '/scenario_*.yaml')
        if len(vnfd_files) == 0 or len(scenario_file) > 1:
            raise Exception("Test '{}' not valid. It must contain an scenario file and at least one vnfd file'".format(
                test_config["test_folder"]))

        #load all vnfd
        for vnfd in vnfd_files:
            with open(vnfd, 'r') as stream:
                vnf_descriptor = yaml.load(stream)

            vnfc_list = vnf_descriptor['vnf']['VNFC']
            for vnfc in vnfc_list:
                vnfc['image name'] = test_config["image_name"]
                devices = vnfc.get('devices',[])
                for device in devices:
                    if device['type'] == 'disk' and 'image name' in device:
                        device['image name'] = test_config["image_name"]

            logger.debug("VNF descriptor: {}".format(vnf_descriptor))
            vnf = test_config["client"].create_vnf(descriptor=vnf_descriptor)
            logger.debug(vnf)
            self.__class__.to_delete_list.insert(0, {"item": "vnf", "function": test_config["client"].delete_vnf,
                                                     "params": {"uuid": vnf['vnf']['uuid']}})

        #load the scenario definition
        with open(scenario_file[0], 'r') as stream:
            scenario_descriptor = yaml.load(stream)
        networks = scenario_descriptor['scenario']['networks']
        networks[test_config["mgmt_net"]] = networks.pop('mgmt')
        logger.debug("Scenario descriptor: {}".format(scenario_descriptor))
        scenario = test_config["client"].create_scenario(descriptor=scenario_descriptor)
        logger.debug(scenario)
        self.__class__.to_delete_list.insert(0,{"item": "scenario", "function": test_config["client"].delete_scenario,
                                 "params":{"uuid": scenario['scenario']['uuid']} })
        self.__class__.scenario_uuid = scenario['scenario']['uuid']

    def test_010_instantiate_scenario(self):
        self.__class__.test_text = "{}.{}. TEST {} {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name,
                                                           test_config["test_folder"])
        self.__class__.test_index += 1

        instance = test_config["client"].create_instance(scenario_id=self.__class__.scenario_uuid,
                                                         name=self.__class__.test_text)
        self.__class__.instance_scenario_uuid = instance['uuid']
        logger.debug(instance)
        self.__class__.to_delete_list.insert(0, {"item": "instance", "function": test_config["client"].delete_instance,
                                  "params": {"uuid": instance['uuid']}})

    def test_020_check_deployent(self):
        self.__class__.test_text = "{}.{}. TEST {} {}".format(test_config["test_number"], self.__class__.test_index,
                                                           inspect.currentframe().f_code.co_name,
                                                           test_config["test_folder"])
        self.__class__.test_index += 1

        if test_config["manual"]:
            raw_input('Scenario has been deployed. Perform manual check and press any key to resume')
            return

        keep_waiting = test_config["timeout"]
        instance_active = False
        while True:
            result = check_instance_scenario_active(self.__class__.instance_scenario_uuid)
            if result[0]:
                break
            elif 'ERROR' in result[1]:
                msg = 'Got error while waiting for the instance to get active: '+result[1]
                logging.error(msg)
                raise Exception(msg)

            if keep_waiting >= 5:
                time.sleep(5)
                keep_waiting -= 5
            elif keep_waiting > 0:
                time.sleep(keep_waiting)
                keep_waiting = 0
            else:
                msg = 'Timeout reached while waiting instance scenario to get active'
                logging.error(msg)
                raise Exception(msg)

    def test_030_clean_deployment(self):
        self.__class__.test_text = "{}.{}. TEST {} {}".format(test_config["test_number"], self.__class__.test_index,
                                                              inspect.currentframe().f_code.co_name,
                                                              test_config["test_folder"])
        self.__class__.test_index += 1
        #At the moment if you delete an scenario right after creating it, in openstack datacenters
        #sometimes scenario ports get orphaned. This sleep is just a dirty workaround
        time.sleep(5)
        for item in self.__class__.to_delete_list:
            response = item["function"](**item["params"])
            logger.debug(response)


def _get_random_string(maxLength):
    '''generates a string with random characters string.letters and string.digits
    with a random length up to maxLength characters. If maxLength is <15 it will be changed automatically to 15
    '''
    prefix = 'testing_'
    min_string = 15
    minLength = min_string - len(prefix)
    if maxLength < min_string: maxLength = min_string
    maxLength -= len(prefix)
    length = random.randint(minLength,maxLength)
    return 'testing_'+"".join([random.choice(string.letters+string.digits) for i in xrange(length)])


def test_vimconnector(args):
    global test_config
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/osm_ro")
    if args.vimtype == "vmware":
        import vimconn_vmware as vim
    elif args.vimtype == "aws":
        import vimconn_aws as vim
    elif args.vimtype == "openstack":
        import vimconn_openstack as vim
    elif args.vimtype == "openvim":
        import vimconn_openvim as vim
    else:
        logger.critical("vimtype '{}' not supported".format(args.vimtype))
        sys.exit(1)
    executed = 0
    failed = 0
    clsmembers = inspect.getmembers(sys.modules[__name__], inspect.isclass)
    # If only want to obtain a tests list print it and exit
    if args.list_tests:
        tests_names = []
        for cls in clsmembers:
            if cls[0].startswith('test_vimconnector'):
                tests_names.append(cls[0])

        msg = "The 'vim' set tests are:\n\t" + ', '.join(sorted(tests_names))
        print(msg)
        logger.info(msg)
        sys.exit(0)

    # Create the list of tests to be run
    code_based_tests = []
    if args.tests:
        for test in args.tests:
            for t in test.split(','):
                matches_code_based_tests = [item for item in clsmembers if item[0] == t]
                if len(matches_code_based_tests) > 0:
                    code_based_tests.append(matches_code_based_tests[0][1])
                else:
                    logger.critical("Test '{}' is not among the possible ones".format(t))
                    sys.exit(1)
    if not code_based_tests:
        # include all tests
        for cls in clsmembers:
            # We exclude 'test_VIM_tenant_operations' unless it is specifically requested by the user
            if cls[0].startswith('test_vimconnector'):
                code_based_tests.append(cls[1])

    logger.debug("tests to be executed: {}".format(code_based_tests))

    # TextTestRunner stream is set to /dev/null in order to avoid the method to directly print the result of tests.
    # This is handled in the tests using logging.
    stream = open('/dev/null', 'w')

    # Run code based tests
    basic_tests_suite = unittest.TestSuite()
    for test in code_based_tests:
        basic_tests_suite.addTest(unittest.makeSuite(test))
    result = unittest.TextTestRunner(stream=stream, failfast=failfast).run(basic_tests_suite)
    executed += result.testsRun
    failed += len(result.failures) + len(result.errors)
    if failfast and failed:
        sys.exit(1)
    if len(result.failures) > 0:
        logger.debug("failures : {}".format(result.failures))
    if len(result.errors) > 0:
        logger.debug("errors : {}".format(result.errors))
    return executed, failed


def test_vim(args):
    global test_config
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/osm_ro")
    import openmanoclient
    executed = 0
    failed = 0
    test_config["client"] = openmanoclient.openmanoclient(
        endpoint_url=args.endpoint_url,
        tenant_name=args.tenant_name,
        datacenter_name=args.datacenter,
        debug=args.debug, logger=test_config["logger_name"])
    clsmembers = inspect.getmembers(sys.modules[__name__], inspect.isclass)
    # If only want to obtain a tests list print it and exit
    if args.list_tests:
        tests_names = []
        for cls in clsmembers:
            if cls[0].startswith('test_VIM'):
                tests_names.append(cls[0])

        msg = "The 'vim' set tests are:\n\t" + ', '.join(sorted(tests_names)) +\
              "\nNOTE: The test test_VIM_tenant_operations will fail in case the used datacenter is type OpenStack " \
              "unless RO has access to the admin endpoint. Therefore this test is excluded by default"
        print(msg)
        logger.info(msg)
        sys.exit(0)

    # Create the list of tests to be run
    code_based_tests = []
    if args.tests:
        for test in args.tests:
            for t in test.split(','):
                matches_code_based_tests = [item for item in clsmembers if item[0] == t]
                if len(matches_code_based_tests) > 0:
                    code_based_tests.append(matches_code_based_tests[0][1])
                else:
                    logger.critical("Test '{}' is not among the possible ones".format(t))
                    sys.exit(1)
    if not code_based_tests:
        # include all tests
        for cls in clsmembers:
            # We exclude 'test_VIM_tenant_operations' unless it is specifically requested by the user
            if cls[0].startswith('test_VIM') and cls[0] != 'test_VIM_tenant_operations':
                code_based_tests.append(cls[1])

    logger.debug("tests to be executed: {}".format(code_based_tests))

    # TextTestRunner stream is set to /dev/null in order to avoid the method to directly print the result of tests.
    # This is handled in the tests using logging.
    stream = open('/dev/null', 'w')

    # Run code based tests
    basic_tests_suite = unittest.TestSuite()
    for test in code_based_tests:
        basic_tests_suite.addTest(unittest.makeSuite(test))
    result = unittest.TextTestRunner(stream=stream, failfast=failfast).run(basic_tests_suite)
    executed += result.testsRun
    failed += len(result.failures) + len(result.errors)
    if failfast and failed:
        sys.exit(1)
    if len(result.failures) > 0:
        logger.debug("failures : {}".format(result.failures))
    if len(result.errors) > 0:
        logger.debug("errors : {}".format(result.errors))
    return executed, failed


def test_deploy(args):
    global test_config
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/osm_ro")
    import openmanoclient
    executed = 0
    failed = 0
    test_config["test_directory"] = os.path.dirname(__file__) + "/RO_tests"
    test_config["image_name"] = args.image_name
    test_config["mgmt_net"] = args.mgmt_net
    test_config["manual"] = args.manual
    test_directory_content = os.listdir(test_config["test_directory"])
    # If only want to obtain a tests list print it and exit
    if args.list_tests:
        msg = "he 'deploy' set tests are:\n\t" + ', '.join(sorted(test_directory_content))
        print(msg)
        logger.info(msg)
        sys.exit(0)

    descriptor_based_tests = []
    # Create the list of tests to be run
    code_based_tests = []
    if args.tests:
        for test in args.tests:
            for t in test.split(','):
                if t in test_directory_content:
                    descriptor_based_tests.append(t)
                else:
                    logger.critical("Test '{}' is not among the possible ones".format(t))
                    sys.exit(1)
    if not descriptor_based_tests:
        # include all tests
        descriptor_based_tests = test_directory_content

    logger.debug("tests to be executed: {}".format(code_based_tests))

    # import openmanoclient from relative path
    test_config["client"] = openmanoclient.openmanoclient(
        endpoint_url=args.endpoint_url,
        tenant_name=args.tenant_name,
        datacenter_name=args.datacenter,
        debug=args.debug, logger=test_config["logger_name"])

    # TextTestRunner stream is set to /dev/null in order to avoid the method to directly print the result of tests.
    # This is handled in the tests using logging.
    stream = open('/dev/null', 'w')
    # This scenario based tests are defined as directories inside the directory defined in 'test_directory'
    for test in descriptor_based_tests:
        test_config["test_folder"] = test
        test_suite = unittest.TestSuite()
        test_suite.addTest(unittest.makeSuite(descriptor_based_scenario_test))
        result = unittest.TextTestRunner(stream=stream, failfast=False).run(test_suite)
        executed += result.testsRun
        failed += len(result.failures) + len(result.errors)
        if failfast and failed:
            sys.exit(1)
        if len(result.failures) > 0:
            logger.debug("failures : {}".format(result.failures))
        if len(result.errors) > 0:
            logger.debug("errors : {}".format(result.errors))

    return executed, failed

if __name__=="__main__":

    parser = ArgumentParser(description='Test RO module')
    parser.add_argument('-v','--version', action='version', help="Show current version",
                             version='%(prog)s version ' + __version__  + ' ' + version_date)

    # Common parameters
    parent_parser = ArgumentParser(add_help=False)
    parent_parser.add_argument('--failfast', help='Stop when a test fails rather than execute all tests',
                      dest='failfast', action="store_true", default=False)
    parent_parser.add_argument('--failed', help='Set logs to show only failed tests. --debug disables this option',
                      dest='failed', action="store_true", default=False)
    default_logger_file = os.path.dirname(__file__)+'/'+os.path.splitext(os.path.basename(__file__))[0]+'.log'
    parent_parser.add_argument('--list-tests', help='List all available tests', dest='list_tests', action="store_true",
                      default=False)
    parent_parser.add_argument('--logger_file', dest='logger_file', default=default_logger_file,
                               help='Set the logger file. By default '+default_logger_file)
    parent_parser.add_argument("-t", '--tenant', dest='tenant_name', default="osm",
                               help="Set the openmano tenant to use for the test. By default 'osm'")
    parent_parser.add_argument('--debug', help='Set logs to debug level', dest='debug', action="store_true")
    parent_parser.add_argument('--timeout', help='Specify the instantiation timeout in seconds. By default 300',
                          dest='timeout', type=int, default=300)
    parent_parser.add_argument('--test', '--tests', help='Specify the tests to run', dest='tests', action="append")

    subparsers = parser.add_subparsers(help='test sets')

    # Deployment test set
    # -------------------
    deploy_parser = subparsers.add_parser('deploy', parents=[parent_parser],
                                          help="test deployment using descriptors at RO_test folder ")
    deploy_parser.set_defaults(func=test_deploy)

    # Mandatory arguments
    mandatory_arguments = deploy_parser.add_argument_group('mandatory arguments')
    mandatory_arguments.add_argument('-d', '--datacenter', required=True, help='Set the datacenter to test')
    mandatory_arguments.add_argument("-i", '--image-name', required=True, dest="image_name",
                                     help='Image name available at datacenter used for the tests')
    mandatory_arguments.add_argument("-n", '--mgmt-net-name', required=True, dest='mgmt_net',
                                     help='Set the vim management network to use for tests')

    # Optional arguments
    deploy_parser.add_argument('-m', '--manual-check', dest='manual', action="store_true", default=False,
                               help='Pause execution once deployed to allow manual checking of the '
                                    'deployed instance scenario')
    deploy_parser.add_argument('-u', '--url', dest='endpoint_url', default='http://localhost:9090/openmano',
                               help="Set the openmano server url. By default 'http://localhost:9090/openmano'")

    # Vimconn test set
    # -------------------
    vimconn_parser = subparsers.add_parser('vimconn', parents=[parent_parser], help="test vimconnector plugin")
    vimconn_parser.set_defaults(func=test_vimconnector)
    # Mandatory arguments
    mandatory_arguments = vimconn_parser.add_argument_group('mandatory arguments')
    mandatory_arguments.add_argument('--vimtype', choices=['vmware', 'aws', 'openstack', 'openvim'], required=True,
                                     help='Set the vimconnector type to test')
    # TODO add mandatory arguments for vimconn test
    # mandatory_arguments.add_argument('-c', '--config', dest='config_param', required=True, help='<HELP>')

    # Optional arguments
    # TODO add optional arguments for vimconn tests
    # vimconn_parser.add_argument("-i", '--image-name', dest='image_name', help='<HELP>'))

    # Datacenter test set
    # -------------------
    vimconn_parser = subparsers.add_parser('vim', parents=[parent_parser], help="test vim")
    vimconn_parser.set_defaults(func=test_vim)

    # Mandatory arguments
    mandatory_arguments = vimconn_parser.add_argument_group('mandatory arguments')
    mandatory_arguments.add_argument('-d', '--datacenter', required=True, help='Set the datacenter to test')

    # Optional arguments
    vimconn_parser.add_argument('-u', '--url', dest='endpoint_url', default='http://localhost:9090/openmano',
                               help="Set the openmano server url. By default 'http://localhost:9090/openmano'")

    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    # print str(args)
    test_config = {}

    # default logger level is INFO. Options --debug and --failed override this, being --debug prioritary
    logger_level = 'INFO'
    if args.debug:
        logger_level = 'DEBUG'
    elif args.failed:
        logger_level = 'WARNING'
    logger_name = os.path.basename(__file__)
    test_config["logger_name"] = logger_name
    logger = logging.getLogger(logger_name)
    logger.setLevel(logger_level)
    failfast = args.failfast

    # Configure a logging handler to store in a logging file
    if args.logger_file:
        fileHandler = logging.FileHandler(args.logger_file)
        formatter_fileHandler = logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s')
        fileHandler.setFormatter(formatter_fileHandler)
        logger.addHandler(fileHandler)

    # Configure a handler to print to stdout
    consoleHandler = logging.StreamHandler(sys.stdout)
    formatter_consoleHandler = logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s')
    consoleHandler.setFormatter(formatter_consoleHandler)
    logger.addHandler(consoleHandler)

    logger.debug('Program started with the following arguments: ' + str(args))

    # set test config parameters
    test_config["timeout"] = args.timeout
    test_config["test_number"] = 1

    executed, failed = args.func(args)

    # Log summary
    logger.warning("Total number of tests: {}; Total number of failures/errors: {}".format(executed, failed))
    sys.exit(1 if failed else 0)
