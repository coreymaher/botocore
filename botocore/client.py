# Copyright 2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import copy
import logging

import botocore.serialize
import botocore.validate
from botocore import waiter, xform_name
from botocore.awsrequest import prepare_request_dict
from botocore.endpoint import EndpointCreator
from botocore.exceptions import ClientError, DataNotFoundError
from botocore.exceptions import OperationNotPageableError
from botocore.hooks import first_non_none_response
from botocore.model import ServiceModel
from botocore.paginate import Paginator
from botocore.signers import RequestSigner
from botocore.utils import CachedProperty


logger = logging.getLogger(__name__)


class ClientCreator(object):
    """Creates client objects for a service."""
    def __init__(self, loader, endpoint_resolver, user_agent, event_emitter,
                 retry_handler_factory, retry_config_translator,
                 response_parser_factory=None):
        self._loader = loader
        self._endpoint_resolver = endpoint_resolver
        self._user_agent = user_agent
        self._event_emitter = event_emitter
        self._retry_handler_factory = retry_handler_factory
        self._retry_config_translator = retry_config_translator
        self._response_parser_factory = response_parser_factory

    def create_client(self, service_name, region_name, is_secure=True,
                      endpoint_url=None, verify=None,
                      credentials=None, scoped_config=None,
                      client_config=None):
        service_model = self._load_service_model(service_name)
        cls = self._create_client_class(service_name, service_model)
        client_args = self._get_client_args(
            service_model, region_name, is_secure, endpoint_url,
            verify, credentials, scoped_config, client_config)
        return cls(**client_args)

    def create_client_class(self, service_name):
        service_model = self._load_service_model(service_name)
        return self._create_client_class(service_name, service_model)

    def _create_client_class(self, service_name, service_model):
        class_attributes = self._create_methods(service_model)
        py_name_to_operation_name = self._create_name_mapping(service_model)
        class_attributes['_PY_TO_OP_NAME'] = py_name_to_operation_name
        bases = [BaseClient]
        self._event_emitter.emit('creating-client-class.%s' % service_name,
                                 class_attributes=class_attributes,
                                 base_classes=bases)
        cls = type(str(service_name), tuple(bases), class_attributes)
        return cls

    def _load_service_model(self, service_name):
        json_model = self._loader.load_service_model(service_name, 'service-2')
        service_model = ServiceModel(json_model, service_name=service_name)
        self._register_retries(service_model)
        return service_model

    def _register_retries(self, service_model):
        endpoint_prefix = service_model.endpoint_prefix

        # First, we load the entire retry config for all services,
        # then pull out just the information we need.
        original_config = self._loader.load_data('_retry')
        if not original_config:
            return

        retry_config = self._retry_config_translator.build_retry_config(
            endpoint_prefix, original_config.get('retry', {}),
            original_config.get('definitions', {}))

        logger.debug("Registering retry handlers for service: %s",
                     service_model.service_name)
        handler = self._retry_handler_factory.create_retry_handler(
            retry_config, endpoint_prefix)
        unique_id = 'retry-config-%s' % endpoint_prefix
        self._event_emitter.register('needs-retry.%s' % endpoint_prefix,
                                     handler, unique_id=unique_id)

    def _get_signature_version_and_region(self, service_model, region_name,
                                          is_secure, scoped_config,
                                          endpoint_url):
        # Get endpoint heuristic overrides before creating the
        # request signer.
        resolver = self._endpoint_resolver
        scheme = 'https' if is_secure else 'http'
        endpoint_config = resolver.construct_endpoint(
            service_model.endpoint_prefix, region_name, scheme=scheme)

        # Signature version override from endpoint.
        signature_version = service_model.signature_version
        if 'signatureVersion' in endpoint_config.get('properties', {}):
            signature_version = endpoint_config[
                'properties']['signatureVersion']

        # Signature overrides from a configuration file.
        if scoped_config is not None:
            service_config = scoped_config.get(service_model.endpoint_prefix)
            if service_config is not None and isinstance(service_config, dict):
                override = service_config.get('signature_version')
                if override:
                    logger.debug(
                        "Switching signature version for service %s "
                        "to version %s based on config file override.",
                        service_model.endpoint_prefix, override)
                    signature_version = override

        # Determine the region name as well
        region_name = self._determine_region_name(
            endpoint_config, region_name, endpoint_url)

        return signature_version, region_name

    def _determine_region_name(self, endpoint_config, region_name=None,
                               endpoint_url=None):
        # This is a helper function to determine region name to use.
        # It will take into account whether the user passes in a region
        # name, whether there is a rule in the endpoint JSON, or
        # an endpoint url was provided.

        # We only support the credentialScope.region in the properties
        # bag right now, so if it's available, it will override the
        # provided region name.
        region_name_override = endpoint_config['properties'].get(
            'credentialScope', {}).get('region')

        if endpoint_url is not None:
            # If an endpoint_url is provided, do not use region name
            # override if a region was provided by the user.
            if region_name is not None:
                region_name_override = None

        if region_name_override is not None:
            # Letting the heuristics rule override the region_name
            # allows for having a default region of something like us-west-2
            # for IAM, but we still will know to use us-east-1 for sigv4.
            region_name = region_name_override

        return region_name

    def _get_client_args(self, service_model, region_name, is_secure,
                         endpoint_url, verify, credentials,
                         scoped_config, client_config):

        protocol = service_model.metadata['protocol']
        serializer = botocore.serialize.create_serializer(
            protocol, include_validation=True)

        event_emitter = copy.copy(self._event_emitter)

        endpoint_creator = EndpointCreator(self._endpoint_resolver,
                                           region_name, event_emitter)
        endpoint = endpoint_creator.create_endpoint(
            service_model, region_name, is_secure=is_secure,
            endpoint_url=endpoint_url, verify=verify,
            response_parser_factory=self._response_parser_factory)

        response_parser = botocore.parsers.create_parser(protocol)

        # Determine what region the user provided either via the
        # region_name argument or the client_config.
        if region_name is None:
            if client_config and client_config.region_name is not None:
                region_name = client_config.region_name

        # Based on what the user provided use the scoped config file
        # to determine if the region is going to change and what
        # signature should be used.
        signature_version, region_name = \
            self._get_signature_version_and_region(
                service_model, region_name, is_secure, scoped_config,
                endpoint_url)

        # Override the signature if the user specifies it in the client
        # config.
        if client_config and client_config.signature_version is not None:
            signature_version = client_config.signature_version

        # Override the user agent if specified in the client config.
        user_agent = self._user_agent
        if client_config is not None:
            if client_config.user_agent is not None:
                user_agent = client_config.user_agent
            if client_config.user_agent_extra is not None:
                user_agent += ' %s' % client_config.user_agent_extra

        signer = RequestSigner(service_model.service_name, region_name,
                               service_model.signing_name,
                               signature_version, credentials,
                               event_emitter)

        # Create a new client config to be passed to the client based
        # on the final values. We do not want the user to be able
        # to try to modify an existing client with a client config.
        client_config = Config(
            region_name=region_name, signature_version=signature_version,
            user_agent=user_agent)

        return {
            'serializer': serializer,
            'endpoint': endpoint,
            'response_parser': response_parser,
            'event_emitter': event_emitter,
            'request_signer': signer,
            'service_model': service_model,
            'loader': self._loader,
            'client_config': client_config
        }

    def _create_methods(self, service_model):
        op_dict = {}
        for operation_name in service_model.operation_names:
            py_operation_name = xform_name(operation_name)
            op_dict[py_operation_name] = self._create_api_method(
                py_operation_name, operation_name, service_model)
        return op_dict

    def _create_name_mapping(self, service_model):
        # py_name -> OperationName, for every operation available
        # for a service.
        mapping = {}
        for operation_name in service_model.operation_names:
            py_operation_name = xform_name(operation_name)
            mapping[py_operation_name] = operation_name
        return mapping

    def _create_api_method(self, py_operation_name, operation_name,
                           service_model):
        def _api_call(self, *args, **kwargs):
            # We're accepting *args so that we can give a more helpful
            # error message than TypeError: _api_call takes exactly
            # 1 argument.
            if args:
                raise TypeError(
                    "%s() only accepts keyword arguments." % py_operation_name)
            # The "self" in this scope is referring to the BaseClient.
            return self._make_api_call(operation_name, kwargs)

        _api_call.__name__ = str(py_operation_name)
        # TODO: docstrings.
        return _api_call


class BaseClient(object):

    # This is actually reassigned with the py->op_name mapping
    # when the client creator creates the subclass.  This value is used
    # because calls such as client.get_paginator('list_objects') use the
    # snake_case name, but we need to know the ListObjects form.
    # xform_name() does the ListObjects->list_objects conversion, but
    # we need the reverse mapping here.
    _PY_TO_OP_NAME = {}

    def __init__(self, serializer, endpoint, response_parser,
                 event_emitter, request_signer, service_model, loader,
                 client_config):
        self._serializer = serializer
        self._endpoint = endpoint
        self._response_parser = response_parser
        self._request_signer = request_signer
        self._cache = {}
        self._loader = loader
        self._client_config = client_config
        self.meta = ClientMeta(event_emitter, self._client_config,
                               endpoint.host, service_model)

        # Register request signing, but only if we have an event
        # emitter. When a client is cloned this is ignored, because
        # the client's ``meta`` will be copied anyway.
        if self.meta.events:
            self.meta.events.register('request-created', self._sign_request)

    @property
    def _service_model(self):
        return self.meta.service_model

    def _make_api_call(self, operation_name, api_params):
        operation_model = self._service_model.operation_model(operation_name)
        request_dict = self._convert_to_request_dict(
            api_params, operation_model)
        http, parsed_response = self._endpoint.make_request(
            operation_model, request_dict)

        self.meta.events.emit(
            'after-call.{endpoint_prefix}.{operation_name}'.format(
                endpoint_prefix=self._service_model.endpoint_prefix,
                operation_name=operation_name),
            http_response=http, parsed=parsed_response,
            model=operation_model
        )

        if http.status_code >= 300:
            raise ClientError(parsed_response, operation_name)
        else:
            return parsed_response

    def _convert_to_request_dict(self, api_params, operation_model):
        # Given the API params provided by the user and the operation_model
        # we can serialize the request to a request_dict.
        operation_name = operation_model.name

        # Emit an event that allows users to modify the parameters at the
        # beginning of the method. It allows handlers to modify existing
        # parameters or return a new set of parameters to use.
        responses = self.meta.events.emit(
            'provide-client-params.{endpoint_prefix}.{operation_name}'.format(
                endpoint_prefix=self._service_model.endpoint_prefix,
                operation_name=operation_name),
            params=api_params, model=operation_model)
        api_params = first_non_none_response(responses, default=api_params)

        event_name = (
            'before-parameter-build.{endpoint_prefix}.{operation_name}')
        self.meta.events.emit(
            event_name.format(
                endpoint_prefix=self._service_model.endpoint_prefix,
                operation_name=operation_name),
            params=api_params, model=operation_model)

        request_dict = self._serializer.serialize_to_request(
            api_params, operation_model)
        prepare_request_dict(request_dict, endpoint_url=self._endpoint.host,
                             user_agent=self._client_config.user_agent)
        self.meta.events.emit(
            'before-call.{endpoint_prefix}.{operation_name}'.format(
                endpoint_prefix=self._service_model.endpoint_prefix,
                operation_name=operation_name),
            model=operation_model, params=request_dict,
            request_signer=self._request_signer
        )
        return request_dict

    def _sign_request(self, operation_name=None, request=None, **kwargs):
        # Sign the request. This fires its own events and will
        # mutate the request as needed.
        self._request_signer.sign(operation_name, request)

    def get_paginator(self, operation_name):
        """Create a paginator for an operation.

        :type operation_name: string
        :param operation_name: The operation name.  This is the same name
            as the method name on the client.  For example, if the
            method name is ``create_foo``, and you'd normally invoke the
            operation as ``client.create_foo(**kwargs)``, if the
            ``create_foo`` operation can be paginated, you can use the
            call ``client.get_paginator("create_foo")``.

        :raise OperationNotPageableError: Raised if the operation is not
            pageable.  You can use the ``client.can_paginate`` method to
            check if an operation is pageable.

        :rtype: L{botocore.paginate.Paginator}
        :return: A paginator object.

        """
        if not self.can_paginate(operation_name):
            raise OperationNotPageableError(operation_name=operation_name)
        else:
            actual_operation_name = self._PY_TO_OP_NAME[operation_name]
            paginator = Paginator(
                getattr(self, operation_name),
                self._cache['page_config'][actual_operation_name])
            return paginator

    def can_paginate(self, operation_name):
        """Check if an operation can be paginated.

        :type operation_name: string
        :param operation_name: The operation name.  This is the same name
            as the method name on the client.  For example, if the
            method name is ``create_foo``, and you'd normally invoke the
            operation as ``client.create_foo(**kwargs)``, if the
            ``create_foo`` operation can be paginated, you can use the
            call ``client.get_paginator("create_foo")``.

        :return: ``True`` if the operation can be paginated,
            ``False`` otherwise.

        """
        if 'page_config' not in self._cache:
            try:
                page_config = self._loader.load_service_model(
                    self._service_model.service_name,
                    'paginators-1',
                    self._service_model.api_version)['pagination']
                self._cache['page_config'] = page_config
            except DataNotFoundError:
                self._cache['page_config'] = {}
        actual_operation_name = self._PY_TO_OP_NAME[operation_name]
        return actual_operation_name in self._cache['page_config']

    def _get_waiter_config(self):
        if 'waiter_config' not in self._cache:
            try:
                waiter_config = self._loader.load_service_model(
                    self._service_model.service_name,
                    'waiters-2',
                    self._service_model.api_version)
                self._cache['waiter_config'] = waiter_config
            except DataNotFoundError:
                self._cache['waiter_config'] = {}
        return self._cache['waiter_config']

    def get_waiter(self, waiter_name):
        config = self._get_waiter_config()
        if not config:
            raise ValueError("Waiter does not exist: %s" % waiter_name)
        model = waiter.WaiterModel(config)
        mapping = {}
        for name in model.waiter_names:
            mapping[xform_name(name)] = name
        if waiter_name not in mapping:
            raise ValueError("Waiter does not exist: %s" % waiter_name)

        return waiter.create_waiter_with_client(
            mapping[waiter_name], model, self)

    @CachedProperty
    def waiter_names(self):
        """Returns a list of all available waiters."""
        config = self._get_waiter_config()
        if not config:
            return []
        model = waiter.WaiterModel(config)
        # Waiter configs is a dict, we just want the waiter names
        # which are the keys in the dict.
        return [xform_name(name) for name in model.waiter_names]


class ClientMeta(object):
    """Holds additional client methods.

    This class holds additional information for clients.  It exists for
    two reasons:

        * To give advanced functionality to clients
        * To namespace additional client attributes from the operation
          names which are mapped to methods at runtime.  This avoids
          ever running into collisions with operation names.

    """

    def __init__(self, events, client_config, endpoint_url, service_model):
        self.events = events
        self._client_config = client_config
        self._endpoint_url = endpoint_url
        self._service_model = service_model

    @property
    def service_model(self):
        return self._service_model

    @property
    def region_name(self):
        return self._client_config.region_name

    @property
    def endpoint_url(self):
        return self._endpoint_url

    @property
    def config(self):
        return self._client_config


class Config(object):
    """Advanced configuration for Botocore clients.

    This class allows you to configure:

        * Region name
        * Signature version
        * User agent
        * User agent extra

    """
    def __init__(self, region_name=None, signature_version=None,
                 user_agent=None, user_agent_extra=None):
        self.region_name = region_name
        self.signature_version = signature_version
        self.user_agent = user_agent
        self.user_agent_extra = user_agent_extra
