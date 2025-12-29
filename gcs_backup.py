"""Google Cloud Storage backup for detection images."""

import logging
import os
import re
from datetime import datetime
from google.cloud import storage

logger = logging.getLogger(__name__)


class GCSBackup:
    """Google Cloud Storage client for uploading detection images."""

    def __init__(self, bucket_name, service_account_json_filename):
        """
        Initialize GCS client.

        Args:
            bucket_name: GCS bucket name
            service_account_json_filename: Filename of service account JSON (relative to config folder)
        """
        self.bucket_name = bucket_name
        try:
            # Resolve relative path to config folder
            config_dir = os.path.join(os.path.dirname(__file__), 'config')
            service_account_json_path = os.path.join(config_dir, service_account_json_filename)

            self.client = storage.Client.from_service_account_json(service_account_json_path)
            self.bucket = self.client.bucket(bucket_name)
            logger.info(f"GCS client initialized for bucket: {bucket_name}")
        except Exception as e:
            logger.error(f"Failed to initialize GCS client: {e}")
            raise

    def sanitize_result(self, result, max_length=50):
        """
        Sanitize Gemini result for use in filename.

        Args:
            result: Raw result string from Gemini
            max_length: Maximum length of sanitized string

        Returns:
            Sanitized string safe for filenames
        """
        # Replace spaces with underscores
        sanitized = result.replace(' ', '_')

        # Keep only alphanumeric and underscores
        sanitized = re.sub(r'[^a-zA-Z0-9_]', '', sanitized)

        # Truncate to max length
        sanitized = sanitized[:max_length]

        # Lowercase for consistency
        sanitized = sanitized.lower()

        return sanitized

    def upload_image(self, image_data, location, result):
        """
        Upload detection image to GCS.

        Args:
            image_data: Binary JPEG data
            location: Detection location (e.g., "front_door")
            result: Detection result from Gemini (e.g., "Person detected near mailbox")

        Returns:
            GCS public URL if upload succeeded, None otherwise
        """
        try:
            # Generate filename: timestamp_location_sanitizedresult.jpg
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            sanitized_result = self.sanitize_result(result)
            filename = f"{timestamp}_{location}_{sanitized_result}.jpg"

            # Upload to GCS
            blob = self.bucket.blob(filename)
            blob.upload_from_string(image_data, content_type='image/jpeg')

            # Construct public GCS URL
            gcs_url = f"https://storage.cloud.google.com/{self.bucket_name}/{filename}"

            logger.info(f"Uploaded image to GCS: {gcs_url}")
            return gcs_url

        except Exception as e:
            logger.error(f"Error uploading to GCS: {e}")
            return None
