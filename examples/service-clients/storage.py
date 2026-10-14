"""S3 calls. The newer botocore model drops the PutObject ACL member."""

import boto3


def upload(body: bytes) -> str:
    client = boto3.client("s3")
    result = client.put_object(Bucket="assets", Key="a.txt", ACL="public-read", Body=body)
    return result["ETag"]
