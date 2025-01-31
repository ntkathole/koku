#
# Copyright 2023 Red Hat Inc.
# SPDX-License-Identifier: Apache-2.0
#
import logging
import os
import pkgutil
from datetime import timedelta
from functools import cached_property

import pandas as pd
from botocore.exceptions import ClientError
from botocore.exceptions import EndpointConnectionError
from django.conf import settings
from django_tenants.utils import schema_context

from api.common import log_json
from api.provider.models import Provider
from masu.database.report_db_accessor_base import ReportDBAccessorBase
from masu.util.aws.common import get_s3_resource
from reporting.models import SubsLastProcessed
from reporting.provider.aws.models import TRINO_LINE_ITEM_TABLE as AWS_TABLE

LOG = logging.getLogger(__name__)

TABLE_MAP = {
    Provider.PROVIDER_AWS: AWS_TABLE,
}


class SUBSDataExtractor(ReportDBAccessorBase):
    def __init__(self, tracing_id, context):
        super().__init__(context["schema"])
        self.provider_type = context["provider_type"].removesuffix("-local")
        self.provider_uuid = context["provider_uuid"]
        self.tracing_id = tracing_id
        self.table = TABLE_MAP.get(self.provider_type)
        self.s3_resource = get_s3_resource(
            settings.S3_SUBS_ACCESS_KEY, settings.S3_SUBS_SECRET, settings.S3_SUBS_REGION
        )
        self.context = context

    @cached_property
    def subs_s3_path(self):
        """The S3 path to be used for a SUBS report upload."""
        return f"{self.schema}/{self.provider_type}/source={self.provider_uuid}/date={self.date_helper.today.date()}"

    def determine_latest_processed_time_for_provider(self, year, month):
        """Determine the latest processed timestamp for a provider for a given month and year."""
        with schema_context(self.schema):
            last_time = SubsLastProcessed.objects.filter(
                source_uuid=self.provider_uuid, year=year, month=month
            ).first()
        if last_time and last_time.latest_processed_time:
            # the stored timestamp is the latest timestamp data was gathered for
            # and we want to gather new data we have not processed yet
            # so we add one second to the last timestamp to ensure the time range processed
            # is all new data
            return last_time.latest_processed_time + timedelta(seconds=1)
        return None

    def determine_end_time(self, year, month):
        sql = (
            f" SELECT MAX(lineitem_usagestartdate) FROM aws_line_items"
            f" WHERE source='{self.provider_uuid}' AND year='{year}' AND month='{month}'"
        )
        latest = self._execute_trino_raw_sql_query(sql, log_ref="insert_subs_last_processed_time")
        return latest[0][0]

    def determine_start_time(self, year, month, month_start):
        """Determines the start time for subs processing"""
        base_time = self.determine_latest_processed_time_for_provider(year, month) or month_start
        created = Provider.objects.get(uuid=self.provider_uuid).created_timestamp
        if base_time < created:
            # this will set the default to start collecting from the midnight hour the day prior to source creation
            return created.replace(microsecond=0, second=0, minute=0, hour=0) - timedelta(days=1)
        return base_time

    def determine_line_item_count(self, where_clause):
        """Determine the number of records in the table that have not been processed and match the criteria"""
        table_count_sql = f"SELECT count(*) FROM {self.schema}.{self.table} {where_clause}"
        count = self._execute_trino_raw_sql_query(table_count_sql, log_ref="determine_subs_processing_count")
        return count[0][0]

    def determine_where_clause(self, latest_processed_time, end_time, year, month):
        """Determine the where clause to use when processing subs data"""
        return (
            f"WHERE source='{self.provider_uuid}' AND year='{year}' AND month='{month}' AND"
            " lineitem_productcode = 'AmazonEC2' AND lineitem_lineitemtype IN ('Usage', 'SavingsPlanCoveredUsage') AND"
            " product_vcpu IS NOT NULL AND strpos(resourcetags, 'com_redhat_rhel') > 0 AND"
            f" lineitem_usagestartdate > TIMESTAMP '{latest_processed_time}' AND"
            f" lineitem_usagestartdate <= TIMESTAMP '{end_time}'"
        )

    def update_latest_processed_time(self, year, month, end_time):
        """Update the latest processing time for a provider"""
        with schema_context(self.schema):
            subs_obj, _ = SubsLastProcessed.objects.get_or_create(
                source_uuid_id=self.provider_uuid, year=year, month=month
            )
            subs_obj.latest_processed_time = end_time
            subs_obj.save()

    def extract_data_to_s3(self, month_start):
        """Process new subs related line items from reports to S3."""
        LOG.info(log_json(self.tracing_id, msg="beginning subs rhel extraction", context=self.context))
        month = month_start.strftime("%m")
        year = month_start.strftime("%Y")
        start_time = self.determine_start_time(year, month, month_start)
        end_time = self.determine_end_time(year, month)
        where_clause = self.determine_where_clause(start_time, end_time, year, month)
        total_count = self.determine_line_item_count(where_clause)
        LOG.debug(
            log_json(
                self.tracing_id,
                msg=f"identified {total_count} matching records for metered rhel",
                context=self.context,
            )
        )
        upload_keys = []
        filename = f"subs_{self.tracing_id}_"
        sql_file = f"trino_sql/{self.provider_type.lower()}_subs_summary.sql"
        query_sql = pkgutil.get_data("subs", sql_file)
        query_sql = query_sql.decode("utf-8")
        for i, offset in enumerate(range(0, total_count, settings.PARQUET_PROCESSING_BATCH_SIZE)):
            sql_params = {
                "schema": self.schema,
                "provider_uuid": self.provider_uuid,
                "year": year,
                "month": month,
                "start_time": start_time,
                "end_time": end_time,
                "offset": offset,
                "limit": settings.PARQUET_PROCESSING_BATCH_SIZE,
            }
            results, description = self._execute_trino_raw_sql_query_with_description(
                query_sql, sql_params=sql_params, log_ref=f"{self.provider_type.lower()}_subs_summary.sql"
            )

            # The format for the description is:
            # [(name, type_code, display_size, internal_size, precision, scale, null_ok)]
            # col[0] grabs the column names from the query results
            cols = [col[0] for col in description]

            upload_keys.append(self.copy_data_to_subs_s3_bucket(results, cols, f"{filename}{i}.csv"))
        self.update_latest_processed_time(year, month, end_time)
        LOG.info(
            log_json(
                self.tracing_id,
                msg=f"{len(upload_keys)} file(s) uploaded to s3 for rhel metering",
                context=self.context,
            )
        )
        return upload_keys

    def copy_data_to_subs_s3_bucket(self, data, cols, filename):
        my_df = pd.DataFrame(data)
        my_df.to_csv(filename, header=cols, index=False)
        with open(filename, "rb") as fin:
            try:
                upload_key = f"{self.subs_s3_path}/{filename}"
                s3_obj = {"bucket_name": settings.S3_SUBS_BUCKET_NAME, "key": upload_key}
                upload = self.s3_resource.Object(**s3_obj)
                upload.upload_fileobj(fin)
            except (EndpointConnectionError, ClientError) as err:
                msg = f"unable to copy data to {upload_key}, bucket {settings.S3_SUBS_BUCKET_NAME}. Reason: {str(err)}"
                LOG.warning(log_json(self.tracing_id, msg=msg, context=self.context))
                return
        os.remove(filename)
        return upload_key
