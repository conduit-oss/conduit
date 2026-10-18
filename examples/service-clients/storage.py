"""S3 calls. The service model shipped with the client is what gets compared."""

import boto3


def upload(body: bytes) -> str:
    client = boto3.client("s3")
    result = client.put_object(Bucket="assets", Key="a.txt", ACL="public-read", Body=body)
    return result["ETag"]
