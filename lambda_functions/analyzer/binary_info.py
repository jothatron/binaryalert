"""Keeps track of all information associated with and computed about a binary."""
import os
import tempfile
import time
from typing import Any, Dict, Set
import uuid

if __package__:
    # Imported by unit tests or other external code.
    from lambda_functions.analyzer import analyzer_aws_lib, file_hash
    from lambda_functions.analyzer.common import LOGGER
    from lambda_functions.analyzer.yara_analyzer import YaraAnalyzer
else:
    import analyzer_aws_lib
    from common import LOGGER
    import file_hash
    from yara_analyzer import YaraAnalyzer


class BinaryInfo(object):
    """Organizes the analysis of a single binary blob in S3."""

    def __init__(self, bucket_name: str, object_key: str, yara_analyzer: YaraAnalyzer):
        """Create a new BinaryInfo.

        Args:
            bucket_name: S3 bucket name.
            object_key: S3 object key.
            yara_analyzer: Analyzer built from a compiled rules file.
        """
        self.bucket_name = bucket_name
        self.object_key = object_key
        self.s3_identifier = 'S3:{}:{}'.format(bucket_name, object_key)

        self.download_path = os.path.join(
            tempfile.gettempdir(), 'binaryalert_{}'.format(uuid.uuid4()))
        self.yara_analyzer = yara_analyzer

        # Computed after file download and analysis.
        self.download_time_ms = 0
        self.s3_last_modified = ''
        self.s3_metadata = {}
        self.computed_md5 = None
        self.computed_sha = None
        self.yara_matches = []  # List of yara.Match objects.

    def __str__(self):
        """Use the S3 identifier as the string representation of the binary."""
        return self.s3_identifier

    def _download_from_s3(self):
        """Download binary from S3 and measure elapsed time."""
        LOGGER.debug('Downloading %s to %s', self.object_key, self.download_path)

        start_time = time.time()
        self.s3_last_modified, self.s3_metadata = analyzer_aws_lib.download_from_s3(
            self.bucket_name, self.object_key, self.download_path)
        self.download_time_ms = (time.time() - start_time) * 1000

    def __enter__(self):
        """Download the binary from S3 and run YARA analysis."""
        self._download_from_s3()
        self.computed_sha, self.computed_md5 = file_hash.compute_hashes(self.download_path)

        LOGGER.debug('Running YARA analysis')
        self.yara_matches = self.yara_analyzer.analyze(
            self.download_path, original_target_path=self.filepath
        )

        return self

    def __exit__(self, exception_type, exception_value, traceback):
        """Remove the downloaded binary from local disk."""
        # In Lambda, "os.remove" does not actually remove the file as expected.
        # Thus, we first truncate the file to set its size to 0 before removing it.
        if os.path.isfile(self.download_path):
            with open(self.download_path, 'wb') as file:
                file.truncate()
            os.remove(self.download_path)

    @property
    def matched_rule_ids(self) -> Set[str]:
        """A list of 'yara_file:rule_name' for each YARA match."""
        return set('{}:{}'.format(match.namespace, match.rule) for match in self.yara_matches)

    @property
    def filepath(self) -> str:
        """The filepath from the S3 metadata, if present."""
        return self.s3_metadata.get('filepath', '')

    def save_matches_and_alert(
            self, analyzer_version: int, dynamo_table_name: str, sns_topic_arn: str) -> None:
        """Save match results to Dynamo and publish an alert to SNS if appropriate.

        Args:
            analyzer_version: The currently executing version of the Lambda function.
            dynamo_table_name: Save YARA match results to this Dynamo table.
            sns_topic_arn: Publish match alerts to this SNS topic ARN.
        """
        table = analyzer_aws_lib.DynamoMatchTable(dynamo_table_name)
        needs_alert = table.save_matches(self, analyzer_version)

        # Send alert if appropriate.
        if needs_alert:
            LOGGER.info('Publishing an SNS alert')
            analyzer_aws_lib.publish_alert_to_sns(self, sns_topic_arn)

    def summary(self) -> Dict[str, Any]:
        """Generate a summary dictionary of binary attributes."""
        result = {
            'FileInfo': {
                'MD5': self.computed_md5,
                'S3LastModified': self.s3_last_modified,
                'S3Location': self.s3_identifier,
                'S3Metadata': self.s3_metadata,
                'SHA256': self.computed_sha
            },
            'MatchedRules': {},
            'NumMatchedRules': len(self.yara_matches)
        }

        for index, match in enumerate(self.yara_matches, start=1):
            result['MatchedRules']['Rule{}'.format(index)] = {
                # YARA string IDs, e.g. "$string1"
                'MatchedStrings': list(sorted(set(t[1] for t in match.strings))),
                'Meta': match.meta,
                'RuleFile': match.namespace,
                'RuleName': match.rule,
                'RuleTags': match.tags
            }
        return result
