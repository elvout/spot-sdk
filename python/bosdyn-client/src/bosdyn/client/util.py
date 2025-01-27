# Copyright (c) 2021 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""Helper functions and classes for creating client applications."""

from concurrent import futures
import copy
from deprecated import deprecated
import getpass
import glob
import grpc
import logging
import os
import six
import signal
import time
import threading

import bosdyn.client.server_util
from bosdyn.client.channel import generate_channel_options
from bosdyn.client.auth import InvalidLoginError
from bosdyn.client.exceptions import Error
import google.protobuf.descriptor

_LOGGER = logging.getLogger(__name__)


def cli_login_prompt(username=None, password=None):
    """Interactive CLI for scripting conveniences."""
    if username is None:
        username = six.moves.input('Username for robot: ')
    else:
        name = six.moves.input('Username for robot [{}]: '.format(username))
        if name:
            username = name

    password = password or getpass.getpass()
    return (username, password)


def cli_auth(robot, username=None, password=None):
    """Interactive CLI for authenticating with the robot."""
    successful = False
    while not successful:
        username, password = cli_login_prompt(username, password)
        try:
            robot.authenticate(username, password)
            successful = True
        except (InvalidLoginError, Error) as e:
            _LOGGER.exception(e)


class DedupLoggingMessages(logging.Filter):
    """Logger filter to prevent duplicated messages from being logged.

    Args:
        always_print_logger_levels (set[logging.Level]): A set of logging levels which
                                                    any logged message at that level will
                                                    always be logged.
    """

    def __init__(self, always_print_logger_levels={logging.CRITICAL, logging.ERROR}):
        # Warning level mapped to last message logged.
        self.last_error_message = None
        self.always_print_logger_levels = always_print_logger_levels

    def filter(self, record):
        warning_level = record.levelno
        # Always allow messages above a certain warning level to be logged.
        if warning_level in self.always_print_logger_levels:
            return True

        error_message = record.getMessage()
        # Deduplicate logged messages by preventing a message that was just logged to be sent again.
        if self.last_error_message != error_message and error_message is not None:
            self.last_error_message = error_message
            return True

        return False


def setup_logging(verbose=False, include_dedup_filter=False,
                  always_print_logger_levels={logging.CRITICAL, logging.ERROR}):
    """Setup a basic streaming console handler at the root logger.

    Args:
        verbose (boolean): if False (default) show messages at INFO level and above,
                           if True show messages at DEBUG level and above.
        include_dedup_filter (boolean): If true, the logger includes a filter which
                                        will prevent repeated duplicated messages
                                        from being logged.
        always_print_logger_levels (set[logging.Level]): A set of logging levels which
                                                        any logged message at that level will
                                                        always be logged.
    """
    logger = get_logger()

    if verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    if not logger.handlers:
        streamlog = logging.StreamHandler()
        streamlog.setLevel(level)
        streamlog.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        if include_dedup_filter:
            # Propagate the filter through the handler. logging.Filter does not propagate to other
            # child loggers on its own, and must be attached to the handler.
            streamlog.addFilter(DedupLoggingMessages(always_print_logger_levels))
        logger.addHandler(streamlog)

    if logger.handlers and include_dedup_filter:
        # If a logger has existing handlers, check if the filter is there already. Also check if it is part of the
        # main log already. If not, add it to a new handler.
        filter_exists = None
        for handler in logger.handlers:
            filter_exists = filter_exists or does_dedup_filter_exist(handler,
                                                                     always_print_logger_levels)
        if not filter_exists:
            dedupFilterLog = logging.StreamHandler()
            # Propagate the filter through the handler. logging.Filter does not propagate to other
            # child loggers on its own, and must be attached to the handler.
            dedupFilterLog.addFilter(DedupLoggingMessages(always_print_logger_levels))
            logger.addHandler(dedupFilterLog)

    # Add the level and filter onto just the regular logger as well.
    logger.setLevel(level)
    if include_dedup_filter:
        if not does_dedup_filter_exist(logger, always_print_logger_levels):
            logger.addFilter(DedupLoggingMessages(always_print_logger_levels))


def does_dedup_filter_exist(logger, always_print_logger_levels):
    """Check if the DedupLoggingMessages filter exists for a logger.

    Returns:
        Boolean indicating if the DedupLoggingMessages filter already exists and matches the new parameters.
    """
    for filt in logger.filters:
        if type(
                filt
        ) == DedupLoggingMessages and filt.always_print_logger_levels == always_print_logger_levels:
            return True
    return False


def get_logger():
    return logging.getLogger()


def add_base_arguments(parser):
    """Add hostname argument to parser.

    Args:
        parser: Argument parser object.
    """
    parser.add_argument('hostname', help='Hostname or address of robot,'
                        ' e.g. "beta25-p" or "192.168.80.3"')
    parser.add_argument('-v', '--verbose', action='store_true', help='Print debug-level messages')


def add_common_arguments(parser):
    """Add arguments common to most applications used for authentication.

    Args:
        parser: Argument parser object.
    """
    parser.add_argument('--username', help='User name of account to get credentials for.')
    parser.add_argument('--password', help='Password to get credentials for.')
    add_base_arguments(parser)


def add_payload_credentials_arguments(parser, required=True):
    """Add arguments common to most payload related applications.

    Args:
        parser: Argument parser object.
        required: Require all arguments to be populated.
    """
    parser.add_argument('--guid', required=required, help='Unique GUID of the payload.')
    parser.add_argument('--secret', required=required, help='Secret of the payload.')


def add_service_hosting_arguments(parser):
    """Add arguments common to most applications hosting a GRPC service.

    Args:
        parser: Argument parser object.
    """
    parser.add_argument(
        '--port', default=0, help=
        ('The port number the service can be reached at (Warning: This port cannot be firewalled).'
         ' Defaults to 0, which will assign an ephemeral port'), type=int)


def add_service_endpoint_arguments(parser):
    """Add arguments common to most applications defining a GRPC service endpoint.

    Args:
        parser: Argument parser object.
    """
    add_service_hosting_arguments(parser)
    parser.add_argument(
        '--host-ip', required=True, help='Hostname or address the service can be reached at.'
        ' e.g. "192.168.50.5"')


@deprecated(reason='App tokens are no longer in use. Authorization is now handled via licenses.',
            version='2.0.1', action="always")
def default_app_token_path():
    """Do nothing, this method is kept only to maintain backwards compatibility."""
    return


@deprecated(reason='The GrpcServiceRunner class helper has moved to server_util.py. Please use '
            'bosdyn.client.server_util.GrpcServiceRunner.', version='3.0.0', action="always")
class GrpcServiceRunner(object):
    """A runner to start a gRPC server on a background thread and allow easy cleanup.

    Args:
        service_servicer (custom servicer class derived from ServiceServicer): Servicer that
            defines server behavior.
        add_servicer_to_server_fn (function): Function generated by gRPC compilation that
            attaches the servicer to the gRPC server.
        port (int): The port number the service can be accessed through on the host system.
            Defaults to 0, which will assign an ephemeral port.
        max_send_message_length (int): Max message length (bytes) allowed for messages sent.
        max_receive_message_length (int): Max message length (bytes) allowed for messages received.
        timeout_secs (int): Number of seconds to wait for a clean server shutdown.
        force_sigint_capture (bool): Re-assign the SIGINT handler to default in order to prevent
            other scripts from blocking a clean exit. Defaults to True.
        logger (logging.Logger): Logger to log with.
    """

    def __init__(self, service_servicer, add_servicer_to_server_fn, port=0, max_workers=4,
                 max_send_message_length=None, max_receive_message_length=None, timeout_secs=3,
                 force_sigint_capture=True, logger=None):
        self.logger = logger or _LOGGER
        self.timeout_secs = timeout_secs
        self.force_sigint_capture = force_sigint_capture

        # Use the name of the service_servicer class for print messages.
        self.server_type_name = type(service_servicer).__name__

        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers),
            options=generate_channel_options(max_send_message_length, max_receive_message_length))
        add_servicer_to_server_fn(service_servicer, self.server)
        self.port = self.server.add_insecure_port('[::]:{}'.format(port))
        self.server.start()
        self.logger.info('Started the {} server.'.format(self.server_type_name))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def stop(self):
        self.logger.info("Shutting down the {} server.".format(self.server_type_name))
        shutdown_complete = self.server.stop(None)
        shutdown_complete.wait(self.timeout_secs)

    def run_until_interrupt(self):
        """Spin the thread until a SIGINT is received and then shut down cleanly."""
        if self.force_sigint_capture:
            # Ensure that KeyboardInterrupt is raised on a SIGINT.
            signal.signal(signal.SIGINT, signal.default_int_handler)

        # Monitor for SIGINT and shut down cleanly.
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        self.stop()



populate_response_header = deprecated(
    reason='The populate_response_header helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.populate_response_header.',
    version='3.0.0', action="always")(bosdyn.client.server_util.populate_response_header)

strip_large_bytes_fields = deprecated(
    reason='The strip_large_bytes_fields helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_large_bytes_fields.',
    version='3.0.0', action="always")(bosdyn.client.server_util.strip_large_bytes_fields)

get_bytes_field_whitelist = deprecated(
    reason='The get_bytes_field_whitelist helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.get_bytes_field_allowlist.',
    version='3.0.0', action="always")(bosdyn.client.server_util.get_bytes_field_allowlist)

strip_image_response = deprecated(
    reason='The strip_image_response helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_image_response.', version='3.0.0',
    action="always")(bosdyn.client.server_util.strip_image_response)

strip_get_image_response = deprecated(
    reason='The strip_get_image_response helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_get_image_response.',
    version='3.0.0', action="always")(bosdyn.client.server_util.strip_get_image_response)

strip_local_grid_responses = deprecated(
    reason='The strip_local_grid_responses helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_local_grid_responses.',
    version='3.0.0', action="always")(bosdyn.client.server_util.strip_local_grid_responses)

strip_store_image_request = deprecated(
    reason='The strip_store_image_request helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_store_image_request.',
    version='3.0.0', action="always")(bosdyn.client.server_util.strip_store_image_request)

strip_store_data_request = deprecated(
    reason='The strip_store_data_request helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_store_data_request.',
    version='3.0.0', action="always")(bosdyn.client.server_util.strip_store_data_request)

strip_record_signal_tick = deprecated(
    reason='The strip_record_signal_tick helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_record_signal_tick.',
    version='3.0.0', action="always")(bosdyn.client.server_util.strip_record_signal_tick)

strip_record_data_blob = deprecated(
    reason='The strip_record_data_blob helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_record_data_blob.', version='3.0.0',
    action="always")(bosdyn.client.server_util.strip_record_data_blob)

strip_log_annotation = deprecated(
    reason='The strip_log_annotation helper has moved to '
    'server_util.py. Please use bosdyn.client.server_util.strip_log_annotation.', version='3.0.0',
    action="always")(bosdyn.client.server_util.strip_log_annotation)
