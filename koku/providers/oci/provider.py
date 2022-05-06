#
# Copyright 2022 Red Hat Inc.
# SPDX-License-Identifier: Apache-2.0
#
"""Oracel cloud infrastructure provider implementation to be used by Koku."""
import logging

import oci
from oci.exceptions import ClientError
from requests.exceptions import ConnectionError as OciConnectionError
from rest_framework import serializers

from ..provider_errors import ProviderErrors
from ..provider_interface import ProviderInterface
from api.common import error_obj
from api.models import Provider
from koku.settings import OCI_CONFIG
from masu.config import Config

DATA_DIR = Config.TMP_DIR
LOG = logging.getLogger(__name__)


def _check_cost_report_access(bucket, namespace, region):
    """Check for provider cost and usage report access."""
    # List all cost and usage reports.
    prefix_file = ""

    # Get the list of reports
    # https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/clienvironmentvariables.htm!!!
    config = OCI_CONFIG
    config["region"] = region

    object_storage = oci.object_storage.ObjectStorageClient(config)
    try:
        oci.pagination.list_call_get_all_results(object_storage.list_objects, namespace, bucket, prefix=prefix_file)

    except (ClientError, OciConnectionError) as oci_error:
        key = ProviderErrors.OCI_NO_REPORT_FOUND
        message = f"Unable to obtain cost and usage reports with: {bucket, namespace, region}."
        LOG.warn(msg=message, exc_info=oci_error)
        raise serializers.ValidationError(error_obj(key, message))

    # return a auth friendly format
    return config, namespace, bucket, region


class OCIProvider(ProviderInterface):
    """Provider interface defnition."""

    def name(self):
        """Return name of the provider."""
        return Provider.PROVIDER_OCI

    def cost_usage_source_is_reachable(self, _, data_source):
        """Verify that the bucket exists and is reachable."""

        storage_resource_name = data_source.get("bucket")
        if not storage_resource_name or storage_resource_name.isspace():
            key = ProviderErrors.OCI_BUCKET_MISSING
            message = ProviderErrors.OCI_BUCKET_MISSING_MESSAGE
            raise serializers.ValidationError(error_obj(key, message))

        bucket_namespace = data_source.get("bucket_namespace")
        if not bucket_namespace or bucket_namespace.isspace():
            key = ProviderErrors.OCI_BUCKET_NAMESPACE_MISSING
            message = ProviderErrors.OCI_BUCKET_NAMESPACE_MISSING_MESSAGE
            raise serializers.ValidationError(error_obj(key, message))

        bucket_region = data_source.get("bucket_region")
        if not bucket_region or bucket_region.isspace():
            key = ProviderErrors.OCI_BUCKET_REGION_MISSING
            message = ProviderErrors.OCI_BUCKET_REGION_MISSING_MESSAGE
            raise serializers.ValidationError(error_obj(key, message))

        _check_cost_report_access(bucket=storage_resource_name, namespace=bucket_namespace, region=bucket_region)

        return True

    def infra_type_implementation(self, provider_uuid, tenant):
        """Return infrastructure type."""
        return None

    def infra_key_list_implementation(self, infrastructure_type, schema_name):
        """Return a list of cluster ids on the given infrastructure type."""
        return []
