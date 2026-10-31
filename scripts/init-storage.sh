#!/bin/sh
set -eu
awslocal s3api head-bucket --bucket jobstream-private 2>/dev/null || awslocal s3api create-bucket --bucket jobstream-private
awslocal s3api put-public-access-block --bucket jobstream-private --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
