import os

# Give boto3 a region plus fake credentials so client creation never looks for
# a real profile. Stubber and mocks intercept every call.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.pop("AWS_PROFILE", None)
