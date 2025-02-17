# Copyright 2017 The Forseti Security Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Crawler implementation for gcp resources."""

# pylint: disable=too-many-lines, no-self-use, bad-docstring-quotes

from builtins import str
from builtins import object
import ctypes
from functools import partial
import hashlib
import json
import os

from google.cloud.forseti.common.gcp_api import errors as api_errors
from google.cloud.forseti.common.util import date_time
from google.cloud.forseti.common.util import logger
from google.cloud.forseti.common.util import string_formats
from google.cloud.forseti.services import utils
from google.cloud.forseti.services.inventory.base.gcp import (
    ResourceNotSupported)
from google.cloud.forseti.services.inventory.base import iam_helpers

LOGGER = logger.get_logger(__name__)


def size_t_hash(key):
    """Hash the key using size_t.

    Args:
        key (str): The key to hash.

    Returns:
        str: The hashed key.
    """
    hash_digest = hashlib.blake2b(key.encode()).hexdigest()  # pylint: disable=no-member
    return '%u' % ctypes.c_size_t(int(hash_digest, 16)).value


def from_root_id(client, root_id, root=True):
    """Start the crawling from root if the root type is supported.

    Args:
        client (object): GCP API client.
        root_id (str): id of the root.
        root (bool): Set this as the root resource in the hierarchy.

    Returns:
        Resource: the root resource instance.

    Raises:
        Exception: Unsupported root id.
    """
    root_map = {
        'organizations': ResourceManagerOrganization.fetch,
        'projects': ResourceManagerProject.fetch,
        'folders': ResourceManagerFolder.fetch,
    }

    for prefix, func in root_map.items():
        if root_id.startswith(prefix):
            return func(client, root_id, root=root)
    raise Exception(
        'Unsupported root id, must be one of {}'.format(
            ','.join(list(root_map.keys()))))


def cached(field_name):
    """Decorator to perform caching.

    Args:
        field_name (str): The name of the attribute to cache.

    Returns:
        wrapper: Function wrapper to perform caching.
    """
    field_name = '__cached_{}'.format(field_name)

    def _cached(f):
        """Cache wrapper.

        Args:
            f (func): function to be decorated.

        Returns:
            wrapper: Function wrapper to perform caching.
        """

        def wrapper(*args, **kwargs):
            """Function wrapper to perform caching.

            Args:
                *args: args to be passed to the function.
                **kwargs: kwargs to be passed to the function.

            Returns:
                object: Results of executing f.
            """
            if hasattr(args[0], field_name):
                return getattr(args[0], field_name)
            result = f(*args, **kwargs)
            setattr(args[0], field_name, result)
            return result

        return wrapper

    return _cached


class ResourceFactory(object):
    """ResourceFactory for visitor pattern."""

    def __init__(self, attributes):
        """Initialize.

        Args:
            attributes (dict): attributes for a specific type of resource.
        """
        self.attributes = attributes

    def create_new(self, data, root=False, metadata=None):
        """Create a new instance of a Resource type.

        Args:
            data (str): raw data.
            root (Resource): root of this resource.
            metadata (AssetMetadata): asset metadata.

        Returns:
            Resource: Resource instance.
        """
        attrs = self.attributes
        cls = attrs['cls']
        return cls(data, root=root, metadata=metadata, **attrs)


# pylint: disable=too-many-instance-attributes, too-many-public-methods
class Resource(object):
    """The base Resource class."""

    def __init__(self, data, root=False,
                 contains=None, metadata=None, **kwargs):
        """Initialize.

        Args:
            data (dict): raw data.
            root (Resource): the root of this crawling.
            contains (list): child types to crawl.
            metadata (AssetMetadata): Asset metadata.
            **kwargs (dict): arguments.
        """
        del kwargs  # Unused.
        self._data = data
        self._metadata = metadata
        self._root = root
        self._stack = None
        self._visitor = None
        self._contains = [] if contains is None else contains
        self._warning = []
        self._timestamp = self._utcnow()
        self._inventory_key = None
        self._full_resource_name = None

    @staticmethod
    def _utcnow():
        """Wrapper for datetime.datetime.now() injection.

        Returns:
            datatime: the datetime.
        """
        return date_time.get_utc_now_datetime()

    def __delitem__(self, key):
        """Delete item.

        Args:
            key (str): key of this resource.
        """
        self._data.pop(key, None)

    def __getitem__(self, key):
        """Get Item.

        Args:
            key (str): key of this resource.

        Returns:
            str: data of this resource.

        Raises:
            KeyError: 'key: {}, data: {}'
        """
        try:
            return self._data[key]
        except KeyError:
            raise KeyError('key: {}, data: {}'.format(key, self._data))

    def __setitem__(self, key, value):
        """Set the value of an item.

        Args:
            key (str): key of this resource.
            value (str): value to set on this resource.
        """
        self._data[key] = value

    def set_inventory_key(self, key):
        """Set the inventory unique id for the resource.

        Args:
            key (int): The unique id for the resource from the storage.
        """
        self._inventory_key = key

    def metadata(self):
        """Gets the asset metadata.

        Returns:
            AssetMetadata: Asset metadata.
        """
        return self._metadata

    def inventory_key(self):
        """Gets the inventory key for this resource, if set.

        Returns:
            int: The unique id for the resource in storage.
        """
        return self._inventory_key

    def get_full_resource_name(self):
        """Gets the full unique resource name for this resource.

        Builds the full name on first call and caches it.

        Returns:
            str: The full unique name for this resource.
        """
        if not self._full_resource_name:
            type_name = utils.to_type_name(self.type(), self.key())
            if self._root or not self.parent():
                parent_full_res_name = ''
            else:
                parent_full_res_name = self.parent().get_full_resource_name()

            self._full_resource_name = utils.to_full_resource_name(
                parent_full_res_name, type_name)

        return self._full_resource_name

    @staticmethod
    def type():
        """Get type of this resource.

        Raises:
            NotImplementedError: method not implemented.
        """
        raise NotImplementedError()

    def data(self):
        """Get data on this resource.

        Returns:
            dict: raw data.
        """
        return self._data

    def parent(self):
        """Get parent of this resource.

        Returns:
            Resource: parent of this resource.
        """
        if self._root:
            return self
        try:
            return self._stack[-1]
        except IndexError:
            return None

    def key(self):
        """Get key of this resource.

        Raises:
            NotImplementedError: key method not implemented.
        """
        raise NotImplementedError('Class: {}'.format(self.__class__.__name__))

    def add_warning(self, warning):
        """Add warning on this resource.

        Args:
            warning (str): warning to be added.
        """
        self._warning.append(str(warning))

    def get_warning(self):
        """Get warning on this resource.

        Returns:
            str: warning message.
        """
        return '\n'.join(self._warning)

    # pylint: disable=broad-except
    def try_accept(self, visitor, stack=None):
        """Handle exceptions on the call the accept.

        Args:
            visitor (object): The class implementing the visitor pattern.
            stack (list): The resource stack from the root to immediate parent
                of this resource.
        """
        try:
            self.accept(visitor, stack)
        except Exception as e:
            err_msg = 'Exception raised processing %s: %s' % (self, e)
            LOGGER.exception(err_msg)
            visitor.on_child_error(self.get_full_resource_name(), e)

    def accept(self, visitor, stack=None):
        """Accept of resource in visitor pattern.

        Args:
            visitor (Crawler): visitor instance.
            stack (list): resource hierarchy stack.
        """
        skip_errors = ['Not found',
                       'Unknown project id',
                       'scheduled for deletion']
        stack = [] if not stack else stack
        self._stack = stack

        # Skip the current resource if it's in the excluded_resources list.
        excluded_resources = visitor.config.variables.get(
            'excluded_resources', {})
        cur_resource_repr = set()
        resource_name = '{}/{}'.format(self.type(), self.key())
        cur_resource_repr.add(resource_name)
        if self.type() == 'project':
            # Supports matching on projectNumber.
            project_number = '{}/{}'.format(self.type(), self['projectNumber'])
            cur_resource_repr.add(project_number)
        if cur_resource_repr.intersection(excluded_resources):
            return

        self._visitor = visitor
        visitor.visit(self)

        for yielder_cls in self._contains:
            yielder = yielder_cls(self, visitor.get_client())
            try:
                for resource in yielder.iter():
                    new_stack = stack + [self]

                    # Parallelization for resource subtrees.
                    if resource.should_dispatch():
                        callback = partial(resource.try_accept,
                                           visitor,
                                           new_stack)
                        visitor.dispatch(callback)
                    else:
                        resource.try_accept(visitor, new_stack)
            except Exception as e:
                # Use string phrases and not error codes since error codes
                # can mean multiple things.
                if (isinstance(e, api_errors.ApiExecutionError) and
                        any(error_str in str(e) for error_str
                            in skip_errors)):
                    pass
                else:
                    err_msg = 'Exception raised processing %s: %s' % (self, e)
                    LOGGER.exception(err_msg)
                    self.add_warning(err_msg)
        if self._warning:
            visitor.on_child_error(self.get_full_resource_name(),
                                   self.get_warning())
    # pylint: enable=broad-except

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get iam policy template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('org_policy')
    def get_org_policy(self, client=None):
        """Gets org policy template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('access_policy')
    def get_access_policy(self, client=None):
        """Gets access policy template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('gcs_policy')
    def get_gcs_policy(self, client=None):
        """Get gcs policy template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('sql_policy')
    def get_cloudsql_policy(self, client=None):
        """Get cloudsql policy template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('dataset_policy')
    def get_dataset_policy(self, client=None):
        """Get dataset policy template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('group_members')
    def get_group_members(self, client=None):
        """Get group member template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('billing_info')
    def get_billing_info(self, client=None):
        """Get billing info template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('enabled_apis')
    def get_enabled_apis(self, client=None):
        """Get enabled apis template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    @cached('service_config')
    def get_kubernetes_service_config(self, client=None):
        """Get kubernetes service config method template.

        Args:
            client (object): GCP API client.
        """
        del client  # Unused.
        return None

    def get_timestamp(self):
        """Template for timestamp when the resource object.

        Returns:
            str: a string timestamp when the resource object was created.
        """
        return self._timestamp.strftime(string_formats.TIMESTAMP_UTC_OFFSET)

    def stack(self):
        """Get resource hierarchy stack of this resource.

        Returns:
            list: resource hierarchy stack of this resource.

        Raises:
            Exception: 'Stack not initialized yet'.
        """
        if self._stack is None:
            raise Exception('Stack not initialized yet')
        return self._stack

    def visitor(self):
        """Get visitor on this resource.

        Returns:
            Crawler: visitor on this resource.

        Raises:
            Exception: 'Visitor not initialized yet'.
        """
        if self._visitor is None:
            raise Exception('Visitor not initialized yet')
        return self._visitor

    def should_dispatch(self):
        """Whether resources should run in parallel threads.

        Returns:
            bool: whether this resource should run in parallel threads.
        """
        return False

    def __repr__(self):
        """String Representation.

        Returns:
            str: Resource representation.
        """
        return ('{}<data="{}", parent_resource_type="{}", '
                'parent_resource_id="{}">').format(
                    self.__class__.__name__,
                    json.dumps(self._data, sort_keys=True),
                    self.parent().type(),
                    self.parent().key())
# pylint: enable=too-many-instance-attributes, too-many-public-methods


def resource_class_factory(resource_type, key_field, hash_key=False):
    """Factory function to generate Resource subclasses.

    Args:
        resource_type (str): The static resource type for this subclass.
        key_field (str): The field in the resource data to use as the resource
            unique key.
        hash_key (bool): If true, use a hash of the key field data instead of
            the value of the key field.

    Returns:
        class: A new class object.
    """

    class ResourceSubclass(Resource):
        """Subclass of Resource."""

        @staticmethod
        def type():
            """Get type of this resource.

            Returns:
                str: The static resource type for this subclass.
            """
            return resource_type

        def key(self):
            """Get key of this resource.

            Returns:
                str: key of this resource.
            """
            if hash_key:
                # Resource does not have a globally unique ID, use size_t hash
                # of key data.
                return size_t_hash(self[key_field])

            return self[key_field]

    return ResourceSubclass


def k8_resource_class_factory(resource_type):
    """Factory function to generate Kubernetes Resource subclasses.

    Args:
        resource_type (str): The static Kubernetes resource type for this
        subclass.

    Returns:
        class: A new class object.
    """

    class ResourceSubclass(Resource):
        """Subclass of Resource."""

        @staticmethod
        def type():
            """Get type of this resource.

            Returns:
                str: The static resource type for this subclass.
            """
            return resource_type

        def key(self):
            """Get key of this resource.

            Returns:
                str: key of this resource.
            """
            # Resource does not have a globally unique ID, use size_t hash
            # of uid under metadata key.
            return size_t_hash(self['metadata']['uid'])

    return ResourceSubclass


# Fake composite resource class
class CompositeRootResource(resource_class_factory('composite_root', None)):
    """The Composite Root fake resource."""

    @classmethod
    def create(cls, composite_root_resources):
        """Creates a new composite root.

        Args:
            composite_root_resources (list): The list of resources to crawl
                using a composite root.

        Returns:
            CompositeRootResource: A new instance of the CompositeRootResource
                class.
        """
        data = {'name': 'Composite Root',
                'composite_children': composite_root_resources}
        resource = FACTORIES['composite_root'].create_new(data, root=True)
        return resource

    def key(self):
        """Get key of this resource.

        Returns:
            str: key of this resource.
        """
        return 'root'


# Resource Manager resource classes
class ResourceManagerOrganization(resource_class_factory('organization', None)):
    """The Resource implementation for Organization."""

    @classmethod
    def fetch(cls, client, resource_key, root=True):
        """Get Organization.

        Saves ApiExecutionErrors as warnings.

        Args:
            client (object): GCP API client.
            resource_key (str): resource key to fetch.
            root (bool): Set this as the root resource in the hierarchy.

        Returns:
            Organization: Organization resource.
        """
        try:
            data, metadata = client.fetch_crm_organization(resource_key)
            return FACTORIES['organization'].create_new(
                data, metadata=metadata, root=root)
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Unable to fetch Organization from API %s: %s, creating '
                       'fake resource.' % (resource_key, e))
            LOGGER.warning(err_msg)
            data = {'name': resource_key}
            resource = FACTORIES['organization'].create_new(data, root=root)
            resource.add_warning(err_msg)
            return resource

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get iam policy for this organization.

        Args:
            client (object): GCP API client.

        Returns:
            dict: organization IAM Policy.
        """
        try:
            data, _ = client.fetch_crm_organization_iam_policy(self['name'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get IAM policy for organization %s: %s' %
                       (self.key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None

    @cached('org_policy')
    def get_org_policy(self, client=None):
        """Gets Organization policy for this organization.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Organization Policy.
        """
        try:
            org_policies = []
            org_policies_iter = (
                client.iter_crm_organization_org_policies(self['name']))
            for org_policy in org_policies_iter:
                org_policies.append(org_policy)
            return org_policies
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            LOGGER.warning('Could not get Org policy: %s', e)
            self.add_warning(e)
            return None

    @cached('access_policy')
    def get_access_policy(self, client=None):
        """Gets access policy for this organization.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Access Policy.
        """
        try:
            access_policies = []
            access_policy_iter = (
                client.iter_crm_org_access_policies(self['name']))
            for access_policy in access_policy_iter:
                access_policies.append(access_policy)
            return access_policies
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            LOGGER.warning('Could not get Access Policy: %s', e)
            self.add_warning(e)
            return None

    def has_directory_resource_id(self):
        """Whether this organization has a directoryCustomerId.

        Returns:
            bool: True if the data exists, else False.
        """
        return ('owner' in self._data and
                'directoryCustomerId' in self['owner'])

    def key(self):
        """Get key of this resource.

        Returns:
            str: key of this resource.
        """
        return self['name'].split('/', 1)[-1]


class ResourceManagerAccessPolicy(resource_class_factory('crm_access_policy',
                                                         None)):
    """The Resource implementation for Resource Manager Access Policy."""

    def key(self):
        """Gets key of thisf resource.

        Returns:
            str: key of this resource
        """
        return self['name']


class ResourceManagerAccessLevel(resource_class_factory('crm_access_level',
                                                        'name')):
    """The Resource implementation for Access Level."""


class ResourceManagerServicePerimeter(resource_class_factory(
        'crm_service_perimeter', 'name')):
    """The Resource implementation for Service Perimeter."""


class ResourceManagerOrgPolicy(resource_class_factory('crm_org_policy', None)):
    """The Resource implementation for Resource Manager Organization Policy."""

    def key(self):
        """Get key of this resource.

        Returns:
            str: key of this resource
        """
        if 'constraint' not in self._data:
            # A row is retrieved for each constraint on a resource.
            unique_key = '/'.join([self.parent().type(),
                                   self.parent().key(),
                                   self[0]['constraint']])
        else:
            unique_key = '/'.join([self.parent().type(),
                                   self.parent().key(),
                                   self['constraint']])
        return '%u' % ctypes.c_size_t(hash(unique_key)).value


class ResourceManagerFolder(resource_class_factory('folder', None)):
    """The Resource implementation for Folder."""

    @classmethod
    def fetch(cls, client, resource_key, root=True):
        """Get Folder.

        Args:
            client (object): GCP API client.
            resource_key (str): resource key to fetch.
            root (bool): Set this as the root resource in the hierarchy.

        Returns:
            Folder: Folder resource.
        """
        try:
            data, metadata = client.fetch_crm_folder(resource_key)
            return FACTORIES['folder'].create_new(
                data, metadata=metadata, root=root)
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Unable to fetch Folder from API %s: %s, creating '
                       'fake resource.' % (resource_key, e))
            LOGGER.warning(err_msg)
            data = {'name': resource_key}
            resource = FACTORIES['folder'].create_new(data, root=root)
            resource.add_warning(err_msg)
            return resource

    def key(self):
        """Get key of this resource.

        Returns:
            str: key of this resource.
        """
        return self['name'].split('/', 1)[-1]

    def should_dispatch(self):
        """Folder resources should run in parallel threads.

        Returns:
            bool: whether folder resources should run in parallel threads.
        """
        return True

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get iam policy for this folder.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Folder IAM Policy.
        """
        try:
            data, _ = client.fetch_crm_folder_iam_policy(self['name'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get IAM policy for folder %s: %s' %
                       (self.key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None

    @cached('org_policy')
    def get_org_policy(self, client=None):
        """Gets Organization policy for this folder.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Folder Organization Policy.
        """
        try:
            org_policies = []
            org_policies_iter = (
                client.iter_crm_organization_org_policies(self['name']))
            for org_policy in org_policies_iter:
                org_policies.append(org_policy)
            return org_policies
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            LOGGER.warning('Could not get Org policy: %s', e)
            self.add_warning(e)
            return None


class ResourceManagerProject(resource_class_factory('project', 'projectId')):
    """The Resource implementation for Project."""

    def __init__(self, data, root=False, contains=None, **kwargs):
        """Initialize.

        Args:
            data (str): raw data.
            root (Resource): the root of this crawling.
            contains (list): child types to crawl.
            **kwargs (dict): arguments.
        """
        super(ResourceManagerProject, self).__init__(data, root, contains,
                                                     **kwargs)
        self._enabled_service_names = None

    @classmethod
    def fetch(cls, client, resource_key, root=True):
        """Get Project.

        Args:
            client (object): GCP API client.
            resource_key (str): resource key to fetch.
            root (bool): Set this as the root resource in the hierarchy.

        Returns:
            Project: created project.
        """
        try:
            project_number = resource_key.split('/', 1)[-1]
            data, metadata = client.fetch_crm_project(project_number)
            return FACTORIES['project'].create_new(
                data, metadata=metadata, root=root)
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Unable to fetch Project from API %s: %s, creating '
                       'fake resource.' % (resource_key, e))
            LOGGER.warning(err_msg)
            data = {'name': resource_key}
            resource = FACTORIES['project'].create_new(data, root=root)
            resource.add_warning(err_msg)
            return resource

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get iam policy for this project.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Project IAM Policy.
        """
        if self.enumerable():
            try:
                data, _ = client.fetch_crm_project_iam_policy(
                    project_number=self['projectNumber'])
                return data
            except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
                err_msg = ('Could not get IAM policy for project %s: %s' %
                           (self.key(), e))
                LOGGER.warning(err_msg)
                self.add_warning(err_msg)
                return None

        return {}

    @cached('org_policy')
    def get_org_policy(self, client=None):
        """Gets Organization policy for this project.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Project Organization Policy.
        """
        try:
            org_policies = []
            org_policies_iter = (
                client.iter_crm_organization_org_policies(self['name']))
            for org_policy in org_policies_iter:
                org_policies.append(org_policy)
            return org_policies
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            LOGGER.warning('Could not get Org policy: %s', e)
            self.add_warning(e)
            return None

    @cached('billing_info')
    def get_billing_info(self, client=None):
        """Get billing info.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Project Billing Info resource.
        """
        if self.enumerable():
            try:
                data, _ = client.fetch_billing_project_info(
                    project_number=self['projectNumber'])
                return data
            except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
                err_msg = ('Could not get Billing Info for project %s: %s' %
                           (self.key(), e))
                LOGGER.warning(err_msg)
                self.add_warning(err_msg)
                return None
        return {}

    @cached('enabled_apis')
    def get_enabled_apis(self, client=None):
        """Get project enabled API services.

        Args:
            client (object): GCP API client.

        Returns:
            list: A list of ManagedService resource dicts.
        """
        enabled_apis = []
        if self.enumerable():
            try:
                enabled_apis, _ = client.fetch_services_enabled_apis(
                    project_number=self['projectNumber'])
            except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
                err_msg = ('Could not get Enabled APIs for project %s: %s' %
                           (self.key(), e))
                LOGGER.warning(err_msg)
                self.add_warning(err_msg)

        self._enabled_service_names = frozenset(
            (api.get('config', {}).get('name') for api in enabled_apis))

        return enabled_apis

    def should_dispatch(self):
        """Project resources should run in parallel threads.

        Returns:
            bool: whether project resources should run in parallel threads.
        """
        return True

    def enumerable(self):
        """Check if this project is enumerable.

        Returns:
            bool: if this project is enumerable.
        """
        return self['lifecycleState'] == 'ACTIVE'

    def billing_enabled(self):
        """Check if billing is configured.

        Returns:
            bool: if billing is enabled on the project.
        """
        if self.get_billing_info():
            return self.get_billing_info().get('billingEnabled', False)

        # If status is unknown, always return True so other APIs aren't blocked.
        return True

    def is_api_enabled(self, service_name):
        """Returns True if the API service is enabled on the project.

        Args:
            service_name (str): The API service name to check.

        Returns:
            bool: whether a service api is enabled
        """
        if self._enabled_service_names:
            return service_name in self._enabled_service_names

        # If status is unknown, always return True so other APIs aren't blocked.
        return True

    def bigquery_api_enabled(self):
        """Check if the bigquery api is enabled.

        Returns:
            bool: if this API service is enabled on the project.
        """
        # Bigquery API depends on billing being enabled
        return (self.billing_enabled() and
                self.is_api_enabled('bigquery-json.googleapis.com'))

    def compute_api_enabled(self):
        """Check if the compute api is enabled.

        Returns:
            bool: if this API service is enabled on the project.
        """
        # Compute API depends on billing being enabled
        return (self.billing_enabled() and
                self.is_api_enabled('compute.googleapis.com'))

    def container_api_enabled(self):
        """Check if the container api is enabled.

        Returns:
            bool: if this API service is enabled on the project.
        """
        # Compute API depends on billing being enabled
        return (self.billing_enabled() and
                self.is_api_enabled('container.googleapis.com'))

    def storage_api_enabled(self):
        """whether storage api is enabled.

        Returns:
            bool: if this API service is enabled on the project.
        """
        return self.is_api_enabled('storage-component.googleapis.com')


class ResourceManagerLien(resource_class_factory('lien', None)):
    """The Resource implementation for Resource Manager Lien."""

    def key(self):
        """Get key of this resource.

        Returns:
            str: key of this resource
        """
        return self['name'].split('/')[-1]


# AppEngine resource classes
class AppEngineApp(resource_class_factory('appengine_app', 'name',
                                          hash_key=True)):
    """The Resource implementation for AppEngine App."""


class AppEngineService(resource_class_factory('appengine_service', 'name',
                                              hash_key=True)):
    """The Resource implementation for AppEngine Service."""


class AppEngineVersion(resource_class_factory('appengine_version', 'name',
                                              hash_key=True)):
    """The Resource implementation for AppEngine Version."""


class AppEngineInstance(resource_class_factory('appengine_instance', 'name',
                                               hash_key=True)):
    """The Resource implementation for AppEngine Instance."""


# Bigquery resource classes
class BigqueryDataSet(resource_class_factory('dataset', 'id')):
    """The Resource implementation for Bigquery DataSet."""

    def _set_cache(self, field_name, value):
        """Manually set a cache value if it isn't already set.

        Args:
            field_name (str): The name of the attribute to cache.
            value (str): The value to cache.
        """
        field_name = '__cached_{}'.format(field_name)
        if not hasattr(self, field_name) or getattr(self, field_name) is None:
            setattr(self, field_name, value)

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """IAM policy for this Dataset.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Dataset Policy.
        """
        try:
            iam_policy, _ = client.fetch_bigquery_iam_policy(
                self.parent()['projectId'],
                self.parent()['projectNumber'],
                self['datasetReference']['datasetId'])
            dataset_policy = iam_helpers.convert_iam_to_bigquery_policy(
                iam_policy)
            self._set_cache('dataset_policy', dataset_policy)
            return iam_policy
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Dataset IAM Policy for %s in project %s: '
                       '%s' % (self.key(), self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None

    @cached('dataset_policy')
    def get_dataset_policy(self, client=None):
        """Dataset policy for this Dataset.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Dataset Policy.
        """
        try:
            dataset_policy, _ = client.fetch_bigquery_dataset_policy(
                self.parent()['projectId'],
                self.parent()['projectNumber'],
                self['datasetReference']['datasetId'])
            iam_policy = iam_helpers.convert_bigquery_policy_to_iam(
                dataset_policy, self.parent()['projectId'])
            self._set_cache('iam_policy', iam_policy)
            return dataset_policy
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Dataset Policy for %s in project %s: '
                       '%s' % (self.key(), self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


# BigqueryTable resource classes
class BigqueryTable(resource_class_factory('bigquery_table', 'id')):
    """The Resource implementation for bigquery table."""


# Bigtable resource classes
class BigtableCluster(resource_class_factory('bigtable_cluster', 'name',
                                             hash_key=True)):
    """The Resource implementation for Bigtable Cluster."""


class BigtableInstance(resource_class_factory('bigtable_instance', 'name',
                                              hash_key=True)):
    """The Resource implementation for Bigtable Instance."""

    @property
    def instance_id(self):
        """Get instance id of the Bigtable Instance

        Returns:
            str: id of this resource.
        """
        return self['name'].split('/')[-1]


class BigtableTable(resource_class_factory('bigtable_table', 'name',
                                           hash_key=True)):
    """The Resource implementation for Bigtable Table."""


# Billing resource classes
class BillingAccount(resource_class_factory('billing_account', None)):
    """The Resource implementation for BillingAccount."""

    def key(self):
        """Get key of this resource.

        Returns:
            str: key of this resource.
        """
        return self['name'].split('/', 1)[-1]

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get iam policy for this folder.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Billing Account IAM Policy.
        """
        try:
            data, _ = client.fetch_billing_account_iam_policy(self['name'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Billing Account IAM Policy for %s: '
                       '%s' % (self.key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


# CloudSQL resource classes
class CloudSqlInstance(resource_class_factory('cloudsqlinstance', 'selfLink',
                                              hash_key=True)):
    """The Resource implementation for CloudSQL Instance."""


# Compute Engine resource classes
class ComputeAddress(resource_class_factory('compute_address', 'id')):
    """The Resource implementation for Compute Address."""


class ComputeAutoscaler(resource_class_factory('compute_autoscaler', 'id')):
    """The Resource implementation for Compute Autoscaler."""


class ComputeBackendBucket(resource_class_factory('compute_backendbucket',
                                                  'id')):
    """The Resource implementation for Compute Backend Bucket."""


class ComputeBackendService(resource_class_factory('backendservice', 'id')):
    """The Resource implementation for Compute Backend Service."""


class ComputeDisk(resource_class_factory('disk', 'id')):
    """The Resource implementation for Compute Disk."""


class ComputeFirewall(resource_class_factory('firewall', 'id')):
    """The Resource implementation for Compute Firewall."""


class ComputeForwardingRule(resource_class_factory('forwardingrule', 'id')):
    """The Resource implementation for Compute Forwarding Rule."""


class ComputeHealthCheck(resource_class_factory('compute_healthcheck', 'id')):
    """The Resource implementation for Compute HealthCheck."""


class ComputeHttpHealthCheck(resource_class_factory('compute_httphealthcheck',
                                                    'id')):
    """The Resource implementation for Compute HTTP HealthCheck."""


class ComputeHttpsHealthCheck(resource_class_factory('compute_httpshealthcheck',
                                                     'id')):
    """The Resource implementation for Compute HTTPS HealthCheck."""


class ComputeImage(resource_class_factory('image', 'id')):
    """The Resource implementation for Compute Image."""


class ComputeInstance(resource_class_factory('instance', 'id')):
    """The Resource implementation for Compute Instance."""


class ComputeInstanceGroup(resource_class_factory('instancegroup', 'id')):
    """The Resource implementation for Compute InstanceGroup."""


class ComputeInstanceGroupManager(resource_class_factory('instancegroupmanager',
                                                         'id')):
    """The Resource implementation for Compute InstanceGroupManager."""


class ComputeInstanceTemplate(resource_class_factory('instancetemplate', 'id')):
    """The Resource implementation for Compute InstanceTemplate."""


class ComputeInterconnect(resource_class_factory('compute_interconnect', 'id')):
    """The Resource implementation for Compute Interconnect."""


class ComputeInterconnectAttachment(resource_class_factory(
        'compute_interconnect_attachment', 'id')):
    """The Resource implementation for Compute Interconnect Attachment."""


class ComputeLicense(resource_class_factory('compute_license', 'id')):
    """The Resource implementation for Compute License."""


class ComputeNetwork(resource_class_factory('network', 'id')):
    """The Resource implementation for Compute Network."""


class ComputeProject(resource_class_factory('compute_project', 'id')):
    """The Resource implementation for Compute Project."""


class ComputeRouter(resource_class_factory('compute_router', 'id')):
    """The Resource implementation for Compute Router."""


class ComputeSecurityPolicy(resource_class_factory('compute_securitypolicy',
                                                   'id')):
    """The Resource implementation for Compute SecurityPolicy."""


class ComputeSnapshot(resource_class_factory('snapshot', 'id')):
    """The Resource implementation for Compute Snapshot."""


class ComputeSslCertificate(resource_class_factory('compute_sslcertificate',
                                                   'id')):
    """The Resource implementation for Compute SSL Certificate."""


class ComputeSubnetwork(resource_class_factory('subnetwork', 'id')):
    """The Resource implementation for Compute Subnetwork."""


class ComputeTargetHttpProxy(resource_class_factory('compute_targethttpproxy',
                                                    'id')):
    """The Resource implementation for Compute TargetHttpProxy."""


class ComputeTargetHttpsProxy(resource_class_factory('compute_targethttpsproxy',
                                                     'id')):
    """The Resource implementation for Compute TargetHttpsProxy."""


class ComputeTargetInstance(resource_class_factory('compute_targetinstance',
                                                   'id')):
    """The Resource implementation for Compute TargetInstance."""


class ComputeTargetPool(resource_class_factory('compute_targetpool', 'id')):
    """The Resource implementation for Compute TargetPool."""


class ComputeTargetSslProxy(resource_class_factory('compute_targetsslproxy',
                                                   'id')):
    """The Resource implementation for Compute TargetSslProxy."""


class ComputeTargetTcpProxy(resource_class_factory('compute_targettcpproxy',
                                                   'id')):
    """The Resource implementation for Compute TargetTcpProxy."""


class ComputeTargetVpnGateway(resource_class_factory('compute_targetvpngateway',
                                                     'id')):
    """The Resource implementation for Compute TargetVpnGateway."""


class ComputeUrlMap(resource_class_factory('compute_urlmap', 'id')):
    """The Resource implementation for Compute UrlMap."""


class ComputeVpnTunnel(resource_class_factory('compute_vpntunnel', 'id')):
    """The Resource implementation for Compute VpnTunnel."""


# Cloud Dataproc resource classes
class DataprocCluster(resource_class_factory('dataproc_cluster',
                                             'clusterUuid')):
    """The Resource implementation for Dataproc Cluster."""

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Dataproc Cluster IAM policy.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Dataproc Cluster IAM policy.
        """
        try:
            # Dataproc resource does not contain a direct reference to the
            # region name except in an embedded label.
            region = self['labels']['goog-dataproc-location']
            cluster = 'projects/{}/regions/{}/clusters/{}'.format(
                self['projectId'], region, self['clusterName'])
            data, _ = client.fetch_dataproc_cluster_iam_policy(cluster)
            return data
        except (api_errors.ApiExecutionError,
                ResourceNotSupported,
                KeyError,
                TypeError) as e:
            if isinstance(e, TypeError):
                e = 'Cluster has no labels.'
            err_msg = ('Could not get Dataproc cluster IAM Policy for %s in '
                       'project %s: %s' % (self.key(), self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


# Cloud DNS resource classes
class DnsManagedZone(resource_class_factory('dns_managedzone', 'id')):
    """The Resource implementation for Cloud DNS ManagedZone."""


class DnsPolicy(resource_class_factory('dns_policy', 'id')):
    """The Resource implementation for Cloud DNS Policy."""


# IAM resource classes
class IamCuratedRole(resource_class_factory('role', 'name')):
    """The Resource implementation for IAM Curated Roles."""

    def parent(self):
        """Curated roles have no parent."""
        return None


class IamRole(resource_class_factory('role', 'name')):
    """The Resource implementation for IAM Roles."""


class IamServiceAccount(resource_class_factory('serviceaccount', 'uniqueId')):
    """The Resource implementation for IAM ServiceAccount."""

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Service Account IAM policy for this service account.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Service Account IAM policy.
        """
        try:
            data, _ = client.fetch_iam_serviceaccount_iam_policy(
                self['name'], self['uniqueId'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Service Account IAM Policy for %s in '
                       'project %s: %s' % (self.key(), self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


class IamServiceAccountKey(resource_class_factory('serviceaccount_key', 'name',
                                                  hash_key=True)):
    """The Resource implementation for IAM ServiceAccountKey."""


# Key Management Service resource classes
class KmsCryptoKey(resource_class_factory('kms_cryptokey', 'name',
                                          hash_key=True)):
    """The Resource implementation for KMS CryptoKey."""

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """KMS CryptoKey IAM policy.

        Args:
            client (object): GCP API client.

        Returns:
            dict: CryptoKey IAM policy.
        """
        try:
            data, _ = client.fetch_kms_cryptokey_iam_policy(self['name'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Crypto Key IAM Policy for %s in project '
                       '%s: %s' % (self['name'], self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


class KmsCryptoKeyVersion(resource_class_factory('kms_cryptokeyversion', 'name',
                                                 hash_key=True)):
    """The Resource implementation for KMS CryptoKeyVersion."""


class KmsKeyRing(resource_class_factory('kms_keyring', 'name',
                                        hash_key=True)):
    """The Resource implementation for KMS KeyRing."""

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """KMS Keyring IAM policy.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Keyring IAM policy.
        """
        try:
            data, _ = client.fetch_kms_keyring_iam_policy(self['name'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Key Ring IAM Policy for %s in project '
                       '%s: %s' % (self['name'], self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


# Kubernetes Engine resource classes
class KubernetesCluster(resource_class_factory('kubernetes_cluster',
                                               'selfLink',
                                               hash_key=True)):
    """The Resource implementation for Kubernetes Cluster."""

    @cached('service_config')
    def get_kubernetes_service_config(self, client=None):
        """Get service config for KubernetesCluster.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Generator of Kubernetes Engine Cluster resources.
        """
        try:
            data, _ = client.fetch_container_serviceconfig(
                self.parent().key(), zone=self.zone(), location=self.location())
            return data
        except ValueError:
            LOGGER.exception('Cluster has no zone or location: %s',
                             self._data)
            return {}
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Cluster service config for %s : %s' %
                       (self['selfLink'], e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None

    def location(self):
        """Get KubernetesCluster location.

        Returns:
            str: KubernetesCluster location.
        """
        try:
            self_link_parts = self['selfLink'].split('/')
            return self_link_parts[self_link_parts.index('locations') + 1]
        except (KeyError, ValueError):
            LOGGER.debug('selfLink not found or contains no locations: %s',
                         self._data)
            return None

    def zone(self):
        """Get KubernetesCluster zone.

        Returns:
            str: KubernetesCluster zone.
        """
        try:
            self_link_parts = self['selfLink'].split('/')
            return self_link_parts[self_link_parts.index('zones') + 1]
        except (KeyError, ValueError):
            LOGGER.debug('selfLink not found or contains no zones: %s',
                         self._data)
            return None

# Kubernetes resource classes


class KubernetesNode(k8_resource_class_factory('kubernetes_node')):
    """The Resource implementation for Kubernetes Node."""


class KubernetesPod(k8_resource_class_factory('kubernetes_pod')):
    """The Resource implementation for Kubernetes Pod."""


class KubernetesNamespace(k8_resource_class_factory('kubernetes_namespace')):
    """The Resource implementation for Kubernetes Namespace."""


class KubernetesRole(k8_resource_class_factory('kubernetes_role')):
    """The Resource implementation for Kubernetes Role."""


class KubernetesRoleBinding(k8_resource_class_factory(
        'kubernetes_rolebinding')):
    """The Resource implementation for Kubernetes RoleBinding."""


class KubernetesClusterRole(k8_resource_class_factory(
        'kubernetes_clusterrole')):
    """The Resource implementation for Kubernetes ClusterRole."""


class KubernetesClusterRoleBinding(k8_resource_class_factory(
        'kubernetes_clusterrolebinding')):
    """The Resource implementation for Kubernetes ClusterRoleBinding."""


# Stackdriver Logging resource classes
class LoggingSink(resource_class_factory('sink', None)):
    """The Resource implementation for Stackdriver Logging sink."""

    def key(self):
        """Get key of this resource.

        Returns:
            str: key of this resource
        """
        sink_name = '/'.join([self.parent().type(), self.parent().key(),
                              self.type(), self['name']])
        return sink_name


# GSuite resource classes
class GsuiteUser(resource_class_factory('gsuite_user', 'id')):
    """The Resource implementation for GSuite User."""


class GsuiteGroup(resource_class_factory('gsuite_group', 'id')):
    """The Resource implementation for GSuite User."""

    def should_dispatch(self):
        """GSuite Groups should always dispatch to another thread.

        Returns:
            bool: Always returns True.
        """
        return True


class GsuiteGroupsSettings(resource_class_factory(
        'gsuite_groups_settings', 'email')):
    """The Resource implementation for GSuite Settings."""


class GsuiteUserMember(resource_class_factory('gsuite_user_member', 'id')):
    """The Resource implementation for GSuite User."""


class GsuiteGroupMember(resource_class_factory('gsuite_group_member', 'id')):
    """The Resource implementation for GSuite User."""


# Cloud Pub/Sub resource classes
class PubsubSubscription(resource_class_factory('pubsub_subscription', 'name',
                                                hash_key=True)):
    """The Resource implementation for PubSub Subscription."""

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get IAM policy for this Pubsub Subscription.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Pubsub Subscription IAM policy.
        """
        try:
            data, _ = client.fetch_pubsub_subscription_iam_policy(self['name'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get PubSub Subscription IAM Policy for %s in '
                       'project %s: %s' %
                       (self['name'], self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


class PubsubTopic(resource_class_factory('pubsub_topic', 'name',
                                         hash_key=True)):
    """The Resource implementation for PubSub Topic."""

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get IAM policy for this Pubsub Topic.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Pubsub Topic IAM policy.
        """
        try:
            data, _ = client.fetch_pubsub_topic_iam_policy(self['name'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get PubSub Topic IAM Policy for %s in '
                       'project %s: %s' %
                       (self['name'], self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


# Service Usage resource classes
class ServiceUsageService(resource_class_factory('service', 'name',
                                                 hash_key=True)):
    """The Resource implementation for Service Usage Service."""


# Cloud Spanner resource classes
class SpannerDatabase(resource_class_factory('spanner_database', 'name',
                                             hash_key=True)):
    """The Resource implementation for Spanner Database."""


class SpannerInstance(resource_class_factory('spanner_instance', 'name',
                                             hash_key=True)):
    """The Resource implementation for Spanner Instance."""


# Cloud storage resource classes
class StorageBucket(resource_class_factory('bucket', 'id')):
    """The Resource implementation for Storage Bucket."""

    @cached('iam_policy')
    def get_iam_policy(self, client=None):
        """Get IAM policy for this Storage bucket.

        Args:
            client (object): GCP API client.

        Returns:
            dict: bucket IAM policy.
        """
        try:
            data, _ = client.fetch_storage_bucket_iam_policy(self.key())
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Bucket IAM Policy for %s in project %s: '
                       '%s' % (self.key(), self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None

    @cached('gcs_policy')
    def get_gcs_policy(self, client=None):
        """Get Bucket Access Control policy for this storage bucket.

        Args:
            client (object): GCP API client.

        Returns:
            list: bucket access controls.
        """
        try:
            # Full projection returns GCS policy with the resource.
            if self['acl']:
                return self['acl']
        except KeyError:
            pass

        try:
            data, _ = client.fetch_storage_bucket_acls(
                self.key(),
                self.parent()['projectId'],
                self.parent()['projectNumber'])
            return data
        except (api_errors.ApiExecutionError, ResourceNotSupported) as e:
            err_msg = ('Could not get Bucket ACL Policy for %s in project %s: '
                       '%s' % (self.key(), self.parent().key(), e))
            LOGGER.warning(err_msg)
            self.add_warning(err_msg)
            return None


class StorageObject(resource_class_factory('storage_object', 'id')):
    """The Resource implementation for Storage Object."""

    def get_gcs_policy(self, client=None):
        """Full projection returns GCS policy with the resource.

        Args:
            client (object): GCP API client.

        Returns:
            dict: Object acl.
        """
        try:
            return self['acl']
        except KeyError:
            return []


class ResourceIterator(object):
    """The Resource iterator template."""

    def __init__(self, resource, client):
        """Initialize.

        Args:
            resource (Resource): The parent resource.
            client (object): GCP API Client.
        """
        self.resource = resource
        self.client = client

    def iter(self):
        """Resource iterator.

        Raises:
            NotImplementedError: Abstract class method not implemented.
        """
        raise NotImplementedError()


class CompositeRootIterator(ResourceIterator):
    """The resource iterator for the fake composite root resource."""

    def iter(self):
        """Creates a new resource child resource for each configured resource.

        Yields:
            Resource: resource returned from client.
        """
        gcp = self.client
        for composite_child in self.resource['composite_children']:
            resource = from_root_id(gcp, composite_child, root=False)
            yield resource


def resource_iter_class_factory(api_method_name,
                                resource_name,
                                api_method_arg_key=None,
                                additional_arg_keys=None,
                                resource_validation_method_name=None,
                                **kwargs):
    """Factory function to generate ResourceIterator subclasses.

    Args:
        api_method_name (str): The method to call on the API client class to
            iterate resources.
        resource_name (str): The name of the resource to create from the
            resource factory.
        api_method_arg_key (str): An optional key from the resource dict to
            lookup for the value to send to the api method.
        additional_arg_keys (list): An optional list of additional keys from the
            resource dict to lookup for the values to send to the api method.
        resource_validation_method_name (str): An optional method name to call
            to validate that the resource supports iterating resources of this
            type.
        **kwargs (dict): Additional keyword args to send to the api method.

    Returns:
        class: A new class object.
    """

    def always_true():
        """Helper function that always returns True.

        Returns:
            bool: True
        """
        return True

    class ResourceIteratorSubclass(ResourceIterator):
        """Subclass of ResourceIterator."""

        def iter(self):
            """Resource iterator.

            Yields:
                Resource: resource returned from client.
            """
            gcp = self.client
            if resource_validation_method_name:
                resource_validation_check = getattr(
                    self.resource, resource_validation_method_name)
            else:
                # Always return True if no check is configured.
                resource_validation_check = always_true

            if resource_validation_check():
                try:
                    iter_method = getattr(gcp, api_method_name)
                    args = []
                    if api_method_arg_key:
                        args.append(self.resource[api_method_arg_key])
                    if additional_arg_keys:
                        args.extend(
                            self.resource[key] for key in additional_arg_keys)
                    for data, metadata in iter_method(*args, **kwargs):
                        yield FACTORIES[resource_name].create_new(
                            data, metadata=metadata)
                except ResourceNotSupported as e:
                    # API client doesn't support this resource, ignore.
                    LOGGER.debug(e)

    return ResourceIteratorSubclass


class AccessLevelIterator(resource_iter_class_factory(
        api_method_name='iter_crm_organization_access_levels',
        resource_name='crm_access_level',
        api_method_arg_key='name')):
    """ The Resource iterator implementation for Access Level."""


class ServicePerimeterIterator(resource_iter_class_factory(
        api_method_name='fetch_crm_organization_service_perimeter',
        resource_name='crm_service_perimeter',
        api_method_arg_key='name')):
    """ The Resource iterator implementation for Service Perimeter."""


class ResourceManagerFolderIterator(resource_iter_class_factory(
        api_method_name='iter_crm_folders',
        resource_name='folder',
        api_method_arg_key='name')):
    """The Resource iterator implementation for Resource Manager Folder."""


class ResourceManagerFolderOrgPolicyIterator(resource_iter_class_factory(
        api_method_name='iter_crm_folder_org_policies',
        resource_name='crm_org_policy',
        api_method_arg_key='name')):
    """The Resource iterator implementation for CRM Folder Org Policies."""


class ResourceManagerOrganizationOrgPolicyIterator(resource_iter_class_factory(
        api_method_name='iter_crm_organization_org_policies',
        resource_name='crm_org_policy',
        api_method_arg_key='name')):
    """The Resource iterator for CRM Organization Org Policies."""


# Project iterator requires looking up parent type, so cannot use class factory.
class ResourceManagerProjectIterator(ResourceIterator):
    """The Resource iterator implementation for Resource Manager Project."""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: Project created
        """
        gcp = self.client
        parent_type = self.resource.type()
        parent_id = self.resource.key()
        try:
            for data, metadata in gcp.iter_crm_projects(
                    parent_type=parent_type, parent_id=parent_id):
                yield FACTORIES['project'].create_new(data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class ResourceManagerProjectOrgPolicyIterator(resource_iter_class_factory(
        api_method_name='iter_crm_project_org_policies',
        resource_name='crm_org_policy',
        api_method_arg_key='projectNumber')):
    """The Resource iterator implementation for CRM Project Org Policies."""


# AppEngine iterators do not support using the class factory.
class AppEngineAppIterator(ResourceIterator):
    """The Resource iterator implementation for AppEngineApp"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: AppEngineApp created
        """
        gcp = self.client
        if self.resource.enumerable():
            try:
                data, metadata = gcp.fetch_gae_app(
                    project_id=self.resource['projectId'])
                if data:
                    yield FACTORIES['appengine_app'].create_new(
                        data, metadata=metadata)
            except ResourceNotSupported as e:
                # API client doesn't support this resource, ignore.
                LOGGER.debug(e)


class AppEngineServiceIterator(ResourceIterator):
    """The Resource iterator implementation for AppEngineService"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: AppEngineService created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_gae_services(
                    project_id=self.resource['id']):
                yield FACTORIES['appengine_service'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class AppEngineVersionIterator(ResourceIterator):
    """The Resource iterator implementation for AppEngineVersion"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: AppEngineVersion created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_gae_versions(
                    project_id=self.resource.parent()['id'],
                    service_id=self.resource['id']):
                yield FACTORIES['appengine_version'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class AppEngineInstanceIterator(ResourceIterator):
    """The Resource iterator implementation for AppEngineInstance"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: AppEngineInstance created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_gae_instances(
                    project_id=self.resource.parent().parent()['id'],
                    service_id=self.resource.parent()['id'],
                    version_id=self.resource['id']):
                yield FACTORIES['appengine_instance'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class BigqueryDataSetIterator(resource_iter_class_factory(
        api_method_name='iter_bigquery_datasets',
        resource_name='bigquery_dataset',
        api_method_arg_key='projectNumber')):
    """The Resource iterator implementation for Bigquery Dataset."""


class BigqueryTableIterator(resource_iter_class_factory(
        api_method_name='iter_bigquery_tables',
        resource_name='bigquery_table',
        api_method_arg_key='datasetReference')):
    """The Resource iterator implementation for Bigquery Table."""


class BigtableClusterIterator(ResourceIterator):
    """The Resource iterator implementation for Bigtable Cluster"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: BigtableCluster created
        """
        gcp = self.client
        if not getattr(self.resource, 'instance_id', ''):
            return

        try:
            for data, metadata in gcp.iter_bigtable_clusters(
                    project_id=self.resource.parent()['projectId'],
                    instance_id=self.resource.instance_id):
                yield FACTORIES['bigtable_cluster'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class BigtableInstanceIterator(resource_iter_class_factory(
        api_method_name='iter_bigtable_instances',
        resource_name='bigtable_instance',
        api_method_arg_key='projectNumber')):
    """The Resource iterator implementation for Bigtable Instance."""


class BigtableTableIterator(ResourceIterator):
    """The Resource iterator implementation for Bigtable Table"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: BigtableTable created
        """
        gcp = self.client
        if not getattr(self.resource, 'instance_id', ''):
            return

        try:
            for data, metadata in gcp.iter_bigtable_tables(
                    project_id=self.resource.parent()['projectId'],
                    instance_id=self.resource.instance_id):
                yield FACTORIES['bigtable_table'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class BillingAccountIterator(resource_iter_class_factory(
        api_method_name='iter_billing_accounts',
        resource_name='billing_account')):
    """The Resource iterator implementation for Billing Account."""


class ResourceManagerOrganizationAccessPolicyIterator(
        resource_iter_class_factory(
            api_method_name='iter_crm_org_access_policies',
            resource_name='crm_access_policy',
            api_method_arg_key='name')):
    """The Resource iterator implementation for Access Policy."""


class CloudSqlInstanceIterator(resource_iter_class_factory(
        api_method_name='iter_cloudsql_instances',
        resource_name='cloudsql_instance',
        api_method_arg_key='projectId',
        additional_arg_keys=['projectNumber'],
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for CloudSQL Instance."""


def compute_iter_class_factory(api_method_name, resource_name):
    """Factory function to generate ResourceIterator subclasses for Compute.

    Args:
        api_method_name (str): The method to call on the API client class to
            iterate resources.
        resource_name (str): The name of the resource to create from the
            resource factory.

    Returns:
        class: A new class object.
    """
    return resource_iter_class_factory(
        api_method_name, resource_name, api_method_arg_key='projectNumber',
        resource_validation_method_name='compute_api_enabled')


class ComputeAddressIterator(compute_iter_class_factory(
        api_method_name='iter_compute_address',
        resource_name='compute_address')):
    """The Resource iterator implementation for Compute Address."""


class ComputeAutoscalerIterator(compute_iter_class_factory(
        api_method_name='iter_compute_autoscalers',
        resource_name='compute_autoscaler')):
    """The Resource iterator implementation for Compute Autoscaler."""


class ComputeBackendBucketIterator(compute_iter_class_factory(
        api_method_name='iter_compute_backendbuckets',
        resource_name='compute_backendbucket')):
    """The Resource iterator implementation for Compute BackendBucket."""


class ComputeBackendServiceIterator(compute_iter_class_factory(
        api_method_name='iter_compute_backendservices',
        resource_name='compute_backendservice')):
    """The Resource iterator implementation for Compute BackendService."""


class ComputeDiskIterator(compute_iter_class_factory(
        api_method_name='iter_compute_disks',
        resource_name='compute_disk')):
    """The Resource iterator implementation for Compute Disk."""


class ComputeFirewallIterator(compute_iter_class_factory(
        api_method_name='iter_compute_firewalls',
        resource_name='compute_firewall')):
    """The Resource iterator implementation for Compute Firewall."""


class ComputeForwardingRuleIterator(compute_iter_class_factory(
        api_method_name='iter_compute_forwardingrules',
        resource_name='compute_forwardingrule')):
    """The Resource iterator implementation for Compute ForwardingRule."""


class ComputeHealthCheckIterator(compute_iter_class_factory(
        api_method_name='iter_compute_healthchecks',
        resource_name='compute_healthcheck')):
    """The Resource iterator implementation for Compute HealthCheck."""


class ComputeHttpHealthCheckIterator(compute_iter_class_factory(
        api_method_name='iter_compute_httphealthchecks',
        resource_name='compute_httphealthcheck')):
    """The Resource iterator implementation for Compute HttpHealthCheck."""


class ComputeHttpsHealthCheckIterator(compute_iter_class_factory(
        api_method_name='iter_compute_httpshealthchecks',
        resource_name='compute_httpshealthcheck')):
    """The Resource iterator implementation for Compute HttpsHealthCheck."""


class ComputeImageIterator(compute_iter_class_factory(
        api_method_name='iter_compute_images',
        resource_name='compute_image')):
    """The Resource iterator implementation for Compute Image."""


# TODO: Refactor IAP scanner to not expect additional data to be included
# with the instancegroup resource.
class ComputeInstanceGroupIterator(ResourceIterator):
    """The Resource iterator implementation for Compute InstanceGroup."""

    def iter(self):
        """Compute InstanceGroup iterator.

        Yields:
            Resource: Compute InstanceGroup resource.
        """
        gcp = self.client
        if self.resource.compute_api_enabled():
            try:
                for data, metadata in gcp.iter_compute_instancegroups(
                        self.resource['projectNumber']):
                    # IAP Scanner expects instance URLs to be included with the
                    # instance groups.
                    try:
                        instance_urls, _ = gcp.fetch_compute_ig_instances(
                            self.resource['projectNumber'],
                            data['name'],
                            zone=os.path.basename(data.get('zone', '')),
                            region=os.path.basename(data.get('region', ''))
                        )
                        data['instance_urls'] = instance_urls
                    except ResourceNotSupported as e:
                        # API client doesn't support this resource, ignore.
                        LOGGER.debug(e)

                    yield FACTORIES['compute_instancegroup'].create_new(
                        data, metadata=metadata)
            except ResourceNotSupported as e:
                # API client doesn't support this resource, ignore.
                LOGGER.debug(e)


class ComputeInstanceGroupManagerIterator(compute_iter_class_factory(
        api_method_name='iter_compute_ig_managers',
        resource_name='compute_instancegroupmanager')):
    """The Resource iterator implementation for Compute InstanceGroupManager."""


class ComputeInstanceIterator(compute_iter_class_factory(
        api_method_name='iter_compute_instances',
        resource_name='compute_instance')):
    """The Resource iterator implementation for Compute Instance."""


class ComputeInstanceTemplateIterator(compute_iter_class_factory(
        api_method_name='iter_compute_instancetemplates',
        resource_name='compute_instancetemplate')):
    """The Resource iterator implementation for Compute InstanceTemplate."""


class ComputeInterconnectIterator(compute_iter_class_factory(
        api_method_name='iter_compute_interconnects',
        resource_name='compute_interconnect')):
    """The Resource iterator implementation for Interconnect."""


class ComputeInterconnectAttachmentIterator(compute_iter_class_factory(
        api_method_name='iter_compute_interconnect_attachments',
        resource_name='compute_interconnect_attachment')):
    """The Resource iterator implementation for InterconnectAttachment."""


class ComputeLicenseIterator(compute_iter_class_factory(
        api_method_name='iter_compute_licenses',
        resource_name='compute_license')):
    """The Resource iterator implementation for Compute License."""


class ComputeNetworkIterator(compute_iter_class_factory(
        api_method_name='iter_compute_networks',
        resource_name='compute_network')):
    """The Resource iterator implementation for Compute Network."""


class ComputeProjectIterator(compute_iter_class_factory(
        api_method_name='iter_compute_project',
        resource_name='compute_project')):
    """The Resource iterator implementation for Compute Project."""


class ComputeRouterIterator(compute_iter_class_factory(
        api_method_name='iter_compute_routers',
        resource_name='compute_router')):
    """The Resource iterator implementation for Compute Router."""


class ComputeSecurityPolicyIterator(compute_iter_class_factory(
        api_method_name='iter_compute_securitypolicies',
        resource_name='compute_securitypolicy')):
    """The Resource iterator implementation for Compute SecurityPolicy."""


class ComputeSnapshotIterator(compute_iter_class_factory(
        api_method_name='iter_compute_snapshots',
        resource_name='compute_snapshot')):
    """The Resource iterator implementation for Compute Snapshot."""


class ComputeSslCertificateIterator(compute_iter_class_factory(
        api_method_name='iter_compute_sslcertificates',
        resource_name='compute_sslcertificate')):
    """The Resource iterator implementation for Compute SSL Certificate."""


class ComputeSubnetworkIterator(compute_iter_class_factory(
        api_method_name='iter_compute_subnetworks',
        resource_name='compute_subnetwork')):
    """The Resource iterator implementation for Compute Subnetwork."""


class ComputeTargetHttpProxyIterator(compute_iter_class_factory(
        api_method_name='iter_compute_targethttpproxies',
        resource_name='compute_targethttpproxy')):
    """The Resource iterator implementation for Compute TargetHttpProxy."""


class ComputeTargetHttpsProxyIterator(compute_iter_class_factory(
        api_method_name='iter_compute_targethttpsproxies',
        resource_name='compute_targethttpsproxy')):
    """The Resource iterator implementation for Compute TargetHttpsProxy."""


class ComputeTargetInstanceIterator(compute_iter_class_factory(
        api_method_name='iter_compute_targetinstances',
        resource_name='compute_targetinstance')):
    """The Resource iterator implementation for Compute TargetInstance."""


class ComputeTargetPoolIterator(compute_iter_class_factory(
        api_method_name='iter_compute_targetpools',
        resource_name='compute_targetpool')):
    """The Resource iterator implementation for Compute TargetPool."""


class ComputeTargetSslProxyIterator(compute_iter_class_factory(
        api_method_name='iter_compute_targetsslproxies',
        resource_name='compute_targetsslproxy')):
    """The Resource iterator implementation for Compute TargetSslProxy."""


class ComputeTargetTcpProxyIterator(compute_iter_class_factory(
        api_method_name='iter_compute_targettcpproxies',
        resource_name='compute_targettcpproxy')):
    """The Resource iterator implementation for Compute TargetTcpProxy."""


class ComputeTargetVpnGatewayIterator(compute_iter_class_factory(
        api_method_name='iter_compute_targetvpngateways',
        resource_name='compute_targetvpngateway')):
    """The Resource iterator implementation for Compute TargetVpnGateway."""


class ComputeUrlMapIterator(compute_iter_class_factory(
        api_method_name='iter_compute_urlmaps',
        resource_name='compute_urlmap')):
    """The Resource iterator implementation for Compute UrlMap."""


class ComputeVpnTunnelIterator(compute_iter_class_factory(
        api_method_name='iter_compute_vpntunnels',
        resource_name='compute_vpntunnel')):
    """The Resource iterator implementation for Compute VpnTunnel."""


class DataprocClusterIterator(resource_iter_class_factory(
        api_method_name='iter_dataproc_clusters',
        resource_name='dataproc_cluster',
        api_method_arg_key='projectId',
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for Cloud Dataproc Cluster."""


class DnsManagedZoneIterator(resource_iter_class_factory(
        api_method_name='iter_dns_managedzones',
        resource_name='dns_managedzone',
        api_method_arg_key='projectNumber',
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for Cloud DNS ManagedZone."""


class DnsPolicyIterator(resource_iter_class_factory(
        api_method_name='iter_dns_policies',
        resource_name='dns_policy',
        api_method_arg_key='projectNumber',
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for Cloud DNS Policy."""


# GSuite iterators do not support using the class factory.
class GsuiteGroupIterator(ResourceIterator):
    """The Resource iterator implementation for Gsuite Group"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: GsuiteGroup created
        """
        gsuite = self.client
        if self.resource.has_directory_resource_id():
            try:
                for data, _ in gsuite.iter_gsuite_groups(
                        self.resource['owner']['directoryCustomerId']):
                    yield FACTORIES['gsuite_group'].create_new(data)
            except ResourceNotSupported as e:
                # API client doesn't support this resource, ignore.
                LOGGER.debug(e)


class GsuiteMemberIterator(ResourceIterator):
    """The Resource iterator implementation for Gsuite Member"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: GsuiteUserMember or GsuiteGroupMember created
        """
        gsuite = self.client
        try:
            for data, _ in gsuite.iter_gsuite_group_members(
                    self.resource['id']):
                if data['type'] == 'USER':
                    yield FACTORIES['gsuite_user_member'].create_new(data)
                elif data['type'] == 'GROUP':
                    yield FACTORIES['gsuite_group_member'].create_new(data)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class GsuiteUserIterator(ResourceIterator):
    """The Resource iterator implementation for Gsuite User"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: GsuiteUser created
        """
        gsuite = self.client
        if self.resource.has_directory_resource_id():
            try:
                for data, _ in gsuite.iter_gsuite_users(
                        self.resource['owner']['directoryCustomerId']):
                    yield FACTORIES['gsuite_user'].create_new(data)
            except ResourceNotSupported as e:
                # API client doesn't support this resource, ignore.
                LOGGER.debug(e)


class GsuiteGroupsSettingsIterator(ResourceIterator):
    """The Resource iterator implementation for Gsuite Group Settings"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: GsuiteGroupsSettings created
        """
        gsuite = self.client
        try:
            data = gsuite.fetch_gsuite_groups_settings(self.resource['email'])
            yield FACTORIES['gsuite_groups_settings'].create_new(data)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class IamOrganizationCuratedRoleIterator(resource_iter_class_factory(
        api_method_name='iter_iam_curated_roles',
        resource_name='iam_curated_role')):
    """The Resource iterator implementation for Organization Curated Role."""


class IamOrganizationRoleIterator(resource_iter_class_factory(
        api_method_name='iter_iam_organization_roles',
        resource_name='iam_role',
        api_method_arg_key='name')):
    """The Resource iterator implementation for IAM Organization Role."""


class IamProjectRoleIterator(resource_iter_class_factory(
        api_method_name='iter_iam_project_roles',
        resource_name='iam_role',
        api_method_arg_key='projectId',
        additional_arg_keys=['projectNumber'],
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for IAM Project Role."""


class IamServiceAccountIterator(resource_iter_class_factory(
        api_method_name='iter_iam_serviceaccounts',
        resource_name='iam_serviceaccount',
        api_method_arg_key='projectId',
        additional_arg_keys=['projectNumber'],
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for IAM ServiceAccount."""


class IamServiceAccountKeyIterator(resource_iter_class_factory(
        api_method_name='iter_iam_serviceaccount_keys',
        resource_name='iam_serviceaccount_key',
        api_method_arg_key='projectId',
        additional_arg_keys=['uniqueId'])):
    """The Resource iterator implementation for IAM ServiceAccount Key."""


class KmsKeyRingIterator(resource_iter_class_factory(
        api_method_name='iter_kms_keyrings',
        resource_name='kms_keyring',
        api_method_arg_key='projectId',
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for KMS KeyRing."""


class KmsCryptoKeyIterator(resource_iter_class_factory(
        api_method_name='iter_kms_cryptokeys',
        resource_name='kms_cryptokey',
        api_method_arg_key='name')):
    """The Resource iterator implementation for KMS CryptoKey."""


class KmsCryptoKeyVersionIterator(resource_iter_class_factory(
        api_method_name='iter_kms_cryptokeyversions',
        resource_name='kms_cryptokeyversion',
        api_method_arg_key='name')):
    """The Resource iterator implementation for KMS CryptoKeyVersion."""


class KubernetesClusterIterator(resource_iter_class_factory(
        api_method_name='iter_container_clusters',
        resource_name='kubernetes_cluster',
        api_method_arg_key='projectNumber',
        resource_validation_method_name='container_api_enabled')):
    """The Resource iterator implementation for Kubernetes Cluster."""


class KubernetesNodeIterator(ResourceIterator):
    """The Resource iterator implementation for KubernetesNode"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: KubernetesCluster created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_kubernetes_nodes(
                    project_id=self.resource.parent()['projectId'],
                    zone=self.resource['zone'],
                    cluster=self.resource['name']):
                yield FACTORIES['kubernetes_node'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class KubernetesPodIterator(ResourceIterator):
    """The Resource iterator implementation for KubernetesPod"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: KubernetesCluster created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_kubernetes_pods(
                    project_id=self.resource.parent().parent()['projectId'],
                    zone=self.resource.parent()['zone'],
                    cluster=self.resource.parent()['name'],
                    namespace=self.resource['metadata']['name']):
                yield FACTORIES['kubernetes_pod'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class KubernetesNamespaceIterator(ResourceIterator):
    """The Resource iterator implementation for KubernetesNamespace"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: KubernetesCluster created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_kubernetes_namespaces(
                    project_id=self.resource.parent()['projectId'],
                    zone=self.resource['zone'],
                    cluster=self.resource['name']):
                yield FACTORIES['kubernetes_namespace'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class KubernetesRoleIterator(ResourceIterator):
    """The Resource iterator implementation for KubernetesRole"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: KubernetesCluster created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_kubernetes_roles(
                    project_id=self.resource.parent().parent()['projectId'],
                    zone=self.resource.parent()['zone'],
                    cluster=self.resource.parent()['name'],
                    namespace=self.resource['metadata']['name']):
                yield FACTORIES['kubernetes_role'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class KubernetesRoleBindingIterator(ResourceIterator):
    """The Resource iterator implementation for KubernetesRoleBinding"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: KubernetesCluster created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_kubernetes_rolebindings(
                    project_id=self.resource.parent().parent()['projectId'],
                    zone=self.resource.parent()['zone'],
                    cluster=self.resource.parent()['name'],
                    namespace=self.resource['metadata']['name']):
                yield FACTORIES['kubernetes_rolebinding'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class KubernetesClusterRoleIterator(ResourceIterator):
    """The Resource iterator implementation for KubernetesClusterRole"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: KubernetesCluster created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_kubernetes_clusterroles(
                    project_id=self.resource.parent()['projectId'],
                    zone=self.resource['zone'],
                    cluster=self.resource['name']):
                yield FACTORIES['kubernetes_clusterrole'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class KubernetesClusterRoleBindingIterator(ResourceIterator):
    """The Resource iterator implementation for KubernetesClusterRoleBinding"""

    def iter(self):
        """Resource iterator.

        Yields:
            Resource: KubernetesCluster created
        """
        gcp = self.client
        try:
            for data, metadata in gcp.iter_kubernetes_clusterrolebindings(
                    project_id=self.resource.parent()['projectId'],
                    zone=self.resource['zone'],
                    cluster=self.resource['name']):
                yield FACTORIES['kubernetes_clusterrolebinding'].create_new(
                    data, metadata=metadata)
        except ResourceNotSupported as e:
            # API client doesn't support this resource, ignore.
            LOGGER.debug(e)


class LoggingBillingAccountSinkIterator(resource_iter_class_factory(
        api_method_name='iter_stackdriver_billing_account_sinks',
        resource_name='logging_sink',
        api_method_arg_key='name')):
    """The Resource iterator implementation for Logging Billing Account Sink."""


class LoggingFolderSinkIterator(resource_iter_class_factory(
        api_method_name='iter_stackdriver_folder_sinks',
        resource_name='logging_sink',
        api_method_arg_key='name')):
    """The Resource iterator implementation for Logging Folder Sink."""


class LoggingOrganizationSinkIterator(resource_iter_class_factory(
        api_method_name='iter_stackdriver_organization_sinks',
        resource_name='logging_sink',
        api_method_arg_key='name')):
    """The Resource iterator implementation for Logging Organization Sink"""


class LoggingProjectSinkIterator(resource_iter_class_factory(
        api_method_name='iter_stackdriver_project_sinks',
        resource_name='logging_sink',
        api_method_arg_key='projectNumber',
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for Logging Project Sink."""


class PubsubSubscriptionIterator(resource_iter_class_factory(
        api_method_name='iter_pubsub_subscriptions',
        resource_name='pubsub_subscription',
        api_method_arg_key='projectId',
        additional_arg_keys=['projectNumber'],
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for PubSub Subscription."""


class PubsubTopicIterator(resource_iter_class_factory(
        api_method_name='iter_pubsub_topics',
        resource_name='pubsub_topic',
        api_method_arg_key='projectId',
        additional_arg_keys=['projectNumber'],
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for PubSub Topic."""


class ResourceManagerProjectLienIterator(resource_iter_class_factory(
        api_method_name='iter_crm_project_liens',
        resource_name='crm_lien',
        api_method_arg_key='projectNumber',
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for Resource Manager Lien."""


class ServiceUsageServiceIterator(resource_iter_class_factory(
        api_method_name='iter_serviceusage_services',
        resource_name='service',
        api_method_arg_key='projectNumber')):
    """The Resource Iterator implementation for Service Usage Services."""


class SpannerDatabaseIterator(resource_iter_class_factory(
        api_method_name='iter_spanner_databases',
        resource_name='spanner_database',
        api_method_arg_key='name')):
    """The Resource iterator implementation for Cloud DNS ManagedZone."""


class SpannerInstanceIterator(resource_iter_class_factory(
        api_method_name='iter_spanner_instances',
        resource_name='spanner_instance',
        api_method_arg_key='projectNumber',
        resource_validation_method_name='enumerable')):
    """The Resource iterator implementation for Cloud DNS Policy."""


class StorageBucketIterator(resource_iter_class_factory(
        api_method_name='iter_storage_buckets',
        resource_name='storage_bucket',
        api_method_arg_key='projectNumber')):
    """The Resource iterator implementation for Storage Bucket."""


class StorageObjectIterator(resource_iter_class_factory(
        api_method_name='iter_storage_objects',
        resource_name='storage_object',
        api_method_arg_key='id')):
    """The Resource iterator implementation for Storage Object."""


FACTORIES = {
    'composite_root': ResourceFactory({
        'dependsOn': [],
        'cls': CompositeRootResource,
        'contains': [
            CompositeRootIterator,
        ]}),

    'organization': ResourceFactory({
        'dependsOn': [],
        'cls': ResourceManagerOrganization,
        'contains': [
            BillingAccountIterator,
            GsuiteGroupIterator,
            GsuiteUserIterator,
            IamOrganizationCuratedRoleIterator,
            IamOrganizationRoleIterator,
            LoggingOrganizationSinkIterator,
            ResourceManagerOrganizationOrgPolicyIterator,
            ResourceManagerFolderIterator,
            ResourceManagerProjectIterator,
            ResourceManagerOrganizationAccessPolicyIterator,
        ]}),

    'folder': ResourceFactory({
        'dependsOn': ['organization'],
        'cls': ResourceManagerFolder,
        'contains': [
            LoggingFolderSinkIterator,
            ResourceManagerFolderOrgPolicyIterator,
            ResourceManagerFolderIterator,
            ResourceManagerProjectIterator,
        ]}),

    'project': ResourceFactory({
        'dependsOn': ['organization', 'folder'],
        'cls': ResourceManagerProject,
        'contains': [
            AppEngineAppIterator,
            BigqueryDataSetIterator,
            BigtableInstanceIterator,
            CloudSqlInstanceIterator,
            ComputeAddressIterator,
            ComputeAutoscalerIterator,
            ComputeBackendBucketIterator,
            ComputeBackendServiceIterator,
            ComputeDiskIterator,
            ComputeFirewallIterator,
            ComputeForwardingRuleIterator,
            ComputeHealthCheckIterator,
            ComputeHttpHealthCheckIterator,
            ComputeHttpsHealthCheckIterator,
            ComputeImageIterator,
            ComputeInstanceGroupIterator,
            ComputeInstanceGroupManagerIterator,
            ComputeInstanceIterator,
            ComputeInstanceTemplateIterator,
            ComputeInterconnectIterator,
            ComputeInterconnectAttachmentIterator,
            ComputeLicenseIterator,
            ComputeNetworkIterator,
            ComputeProjectIterator,
            ComputeRouterIterator,
            ComputeSecurityPolicyIterator,
            ComputeSnapshotIterator,
            ComputeSslCertificateIterator,
            ComputeSubnetworkIterator,
            ComputeTargetHttpProxyIterator,
            ComputeTargetHttpsProxyIterator,
            ComputeTargetInstanceIterator,
            ComputeTargetPoolIterator,
            ComputeTargetSslProxyIterator,
            ComputeTargetTcpProxyIterator,
            ComputeTargetVpnGatewayIterator,
            ComputeUrlMapIterator,
            ComputeVpnTunnelIterator,
            DataprocClusterIterator,
            DnsManagedZoneIterator,
            DnsPolicyIterator,
            IamProjectRoleIterator,
            IamServiceAccountIterator,
            KmsKeyRingIterator,
            KubernetesClusterIterator,
            LoggingProjectSinkIterator,
            PubsubSubscriptionIterator,
            PubsubTopicIterator,
            ResourceManagerProjectLienIterator,
            ResourceManagerProjectOrgPolicyIterator,
            ServiceUsageServiceIterator,
            SpannerInstanceIterator,
            StorageBucketIterator,
        ]}),

    'appengine_app': ResourceFactory({
        'dependsOn': ['project'],
        'cls': AppEngineApp,
        'contains': [
            AppEngineServiceIterator,
        ]}),

    'appengine_service': ResourceFactory({
        'dependsOn': ['appengine_app'],
        'cls': AppEngineService,
        'contains': [
            AppEngineVersionIterator,
        ]}),

    'appengine_version': ResourceFactory({
        'dependsOn': ['appengine_service'],
        'cls': AppEngineVersion,
        'contains': [
            AppEngineInstanceIterator,
        ]}),

    'appengine_instance': ResourceFactory({
        'dependsOn': ['appengine_version'],
        'cls': AppEngineInstance,
        'contains': []}),

    'billing_account': ResourceFactory({
        'dependsOn': ['organization'],
        'cls': BillingAccount,
        'contains': [
            LoggingBillingAccountSinkIterator,
        ]}),

    'bigquery_dataset': ResourceFactory({
        'dependsOn': ['project'],
        'cls': BigqueryDataSet,
        'contains': [
            BigqueryTableIterator
        ]}),

    'bigquery_table': ResourceFactory({
        'dependsOn': ['bigquery_dataset'],
        'cls': BigqueryTable,
        'contains': []}),

    'bigtable_cluster': ResourceFactory({
        'dependsOn': ['bigtable_instance'],
        'cls': BigtableCluster,
        'contains': []}),

    'bigtable_instance': ResourceFactory({
        'dependsOn': ['project'],
        'cls': BigtableInstance,
        'contains': [
            BigtableClusterIterator,
            BigtableTableIterator
        ]}),

    'bigtable_table': ResourceFactory({
        'dependsOn': ['bigtable_instance'],
        'cls': BigtableTable,
        'contains': []}),

    'cloudsql_instance': ResourceFactory({
        'dependsOn': ['project'],
        'cls': CloudSqlInstance,
        'contains': []}),

    'compute_address': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeAddress,
        'contains': []}),

    'compute_autoscaler': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeAutoscaler,
        'contains': []}),

    'compute_backendservice': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeBackendService,
        'contains': []}),

    'compute_backendbucket': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeBackendBucket,
        'contains': []}),

    'compute_disk': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeDisk,
        'contains': []}),

    'compute_firewall': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeFirewall,
        'contains': []}),

    'compute_forwardingrule': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeForwardingRule,
        'contains': []}),

    'compute_healthcheck': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeHealthCheck,
        'contains': []}),

    'compute_httphealthcheck': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeHttpHealthCheck,
        'contains': []}),

    'compute_httpshealthcheck': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeHttpsHealthCheck,
        'contains': []}),

    'compute_image': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeImage,
        'contains': []}),

    'compute_instancegroup': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeInstanceGroup,
        'contains': []}),

    'compute_instancegroupmanager': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeInstanceGroupManager,
        'contains': []}),

    'compute_instance': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeInstance,
        'contains': []}),

    'compute_instancetemplate': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeInstanceTemplate,
        'contains': []}),

    'compute_interconnect': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeInterconnect,
        'contains': []}),

    'compute_interconnect_attachment': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeInterconnectAttachment,
        'contains': []}),

    'compute_license': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeLicense,
        'contains': []}),

    'compute_network': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeNetwork,
        'contains': []}),

    'compute_project': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeProject,
        'contains': []}),

    'compute_router': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeRouter,
        'contains': []}),

    'compute_securitypolicy': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeSecurityPolicy,
        'contains': []}),

    'compute_snapshot': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeSnapshot,
        'contains': []}),

    'compute_sslcertificate': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeSslCertificate,
        'contains': []}),

    'compute_subnetwork': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeSubnetwork,
        'contains': []}),

    'compute_targethttpproxy': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeTargetHttpProxy,
        'contains': []}),

    'compute_targethttpsproxy': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeTargetHttpsProxy,
        'contains': []}),

    'compute_targetinstance': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeTargetInstance,
        'contains': []}),

    'compute_targetpool': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeTargetPool,
        'contains': []}),

    'compute_targetsslproxy': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeTargetSslProxy,
        'contains': []}),

    'compute_targettcpproxy': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeTargetTcpProxy,
        'contains': []}),

    'compute_targetvpngateway': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeTargetVpnGateway,
        'contains': []}),

    'compute_urlmap': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeUrlMap,
        'contains': []}),

    'compute_vpntunnel': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ComputeVpnTunnel,
        'contains': []}),

    'crm_lien': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ResourceManagerLien,
        'contains': []}),

    'crm_org_policy': ResourceFactory({
        'dependsOn': ['folder', 'organization', 'project'],
        'cls': ResourceManagerOrgPolicy,
        'contains': []}),

    'crm_access_policy': ResourceFactory({
        'dependsOn': ['organization'],
        'cls': ResourceManagerAccessPolicy,
        'contains': [
            AccessLevelIterator,
            ServicePerimeterIterator,
        ]}),

    'crm_access_level': ResourceFactory({
        'dependsOn': ['crm_access_policy'],
        'cls': ResourceManagerAccessLevel,
        'contains': []}),

    'crm_service_perimeter': ResourceFactory({
        'dependsOn': ['crm_access_policy'],
        'cls': ResourceManagerServicePerimeter,
        'contains': []}),

    'dataproc_cluster': ResourceFactory({
        'dependsOn': ['project'],
        'cls': DataprocCluster,
        'contains': []}),

    'dns_managedzone': ResourceFactory({
        'dependsOn': ['project'],
        'cls': DnsManagedZone,
        'contains': []}),

    'dns_policy': ResourceFactory({
        'dependsOn': ['project'],
        'cls': DnsPolicy,
        'contains': []}),

    'service': ResourceFactory({
        'dependsOn': ['project'],
        'cls': ServiceUsageService,
        'contains': []}),

    'gsuite_group': ResourceFactory({
        'dependsOn': ['organization'],
        'cls': GsuiteGroup,
        'contains': [
            GsuiteMemberIterator,
            GsuiteGroupsSettingsIterator,
        ]}),

    'gsuite_groups_settings': ResourceFactory({
        'dependsOn': ['gsuite_group'],
        'cls': GsuiteGroupsSettings,
        'contains': []}),

    'gsuite_group_member': ResourceFactory({
        'dependsOn': ['gsuite_group'],
        'cls': GsuiteGroupMember,
        'contains': []}),

    'gsuite_user': ResourceFactory({
        'dependsOn': ['organization'],
        'cls': GsuiteUser,
        'contains': []}),

    'gsuite_user_member': ResourceFactory({
        'dependsOn': ['gsuite_group'],
        'cls': GsuiteUserMember,
        'contains': []}),

    'iam_curated_role': ResourceFactory({
        'dependsOn': [],
        'cls': IamCuratedRole,
        'contains': []}),

    'iam_role': ResourceFactory({
        'dependsOn': ['organization', 'project'],
        'cls': IamRole,
        'contains': []}),

    'iam_serviceaccount': ResourceFactory({
        'dependsOn': ['project'],
        'cls': IamServiceAccount,
        'contains': [
            IamServiceAccountKeyIterator
        ]}),

    'iam_serviceaccount_key': ResourceFactory({
        'dependsOn': ['iam_serviceaccount'],
        'cls': IamServiceAccountKey,
        'contains': []}),

    'kms_keyring': ResourceFactory({
        'dependsOn': ['project'],
        'cls': KmsKeyRing,
        'contains': [
            KmsCryptoKeyIterator
        ]}),

    'kms_cryptokey': ResourceFactory({
        'dependsOn': ['kms_keyring'],
        'cls': KmsCryptoKey,
        'contains': [
            KmsCryptoKeyVersionIterator
        ]}),

    'kms_cryptokeyversion': ResourceFactory({
        'dependsOn': ['kms_cryptokey'],
        'cls': KmsCryptoKeyVersion,
        'contains': []}),

    'kubernetes_cluster': ResourceFactory({
        'dependsOn': ['project'],
        'cls': KubernetesCluster,
        'contains': [
            KubernetesNodeIterator,
            KubernetesNamespaceIterator,
            KubernetesClusterRoleIterator,
            KubernetesClusterRoleBindingIterator,
        ]}),

    'kubernetes_namespace': ResourceFactory({
        'dependsOn': ['kubernetes_cluster'],
        'cls': KubernetesNamespace,
        'contains': [
            KubernetesPodIterator,
            KubernetesRoleIterator,
            KubernetesRoleBindingIterator,
        ]}),

    'kubernetes_node': ResourceFactory({
        'dependsOn': ['kubernetes_cluster'],
        'cls': KubernetesNode,
        'contains': []}),

    'kubernetes_pod': ResourceFactory({
        'dependsOn': ['kubernetes_namespace'],
        'cls': KubernetesPod,
        'contains': []}),

    'kubernetes_role': ResourceFactory({
        'dependsOn': ['kubernetes_namespace'],
        'cls': KubernetesRole,
        'contains': []}),

    'kubernetes_rolebinding': ResourceFactory({
        'dependsOn': ['kubernetes_namespace'],
        'cls': KubernetesRoleBinding,
        'contains': []}),

    'kubernetes_clusterrole': ResourceFactory({
        'dependsOn': ['kubernetes_cluster'],
        'cls': KubernetesClusterRole,
        'contains': []}),

    'kubernetes_clusterrolebinding': ResourceFactory({
        'dependsOn': ['kubernetes_cluster'],
        'cls': KubernetesClusterRoleBinding,
        'contains': []}),

    'logging_sink': ResourceFactory({
        'dependsOn': ['organization', 'folder', 'project'],
        'cls': LoggingSink,
        'contains': []}),

    'pubsub_subscription': ResourceFactory({
        'dependsOn': ['project'],
        'cls': PubsubSubscription,
        'contains': []}),

    'pubsub_topic': ResourceFactory({
        'dependsOn': ['project'],
        'cls': PubsubTopic,
        'contains': []}),

    'spanner_database': ResourceFactory({
        'dependsOn': ['project'],
        'cls': SpannerDatabase,
        'contains': []}),

    'spanner_instance': ResourceFactory({
        'dependsOn': ['project'],
        'cls': SpannerInstance,
        'contains': [
            SpannerDatabaseIterator
        ]}),

    'storage_bucket': ResourceFactory({
        'dependsOn': ['project'],
        'cls': StorageBucket,
        'contains': [
            # StorageObjectIterator
        ]}),

    'storage_object': ResourceFactory({
        'dependsOn': ['bucket'],
        'cls': StorageObject,
        'contains': []}),
}
