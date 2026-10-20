"""Test environment setup.

The handler creates its boto3 client at module scope, so that the connection
is reused across warm invocations instead of being rebuilt on every call. That
is the right thing to do in Lambda, where AWS_REGION is always set by the
runtime -- but it means an import under pytest fails with NoRegionError unless
the environment looks like Lambda.

Setting these here rather than patching the handler keeps the test honest: the
code under test is exactly the code that ships.
"""

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("AWS_REGION", "ap-southeast-2")

# Never let a test reach real AWS, even by accident.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
