#!/bin/bash
# Lets one repo's workflows in its "downloads" GitHub environment publish under one top-level folder and its dev/ drafts.
# Rerunning it updates an existing role's policy.
# Usage: create_publish_role.sh <owner/repo> <folder> <bucket>, e.g. ARK-Electronics/ark_jetson_kernel jetson <bucket>
# Prints the role ARN, which goes in the repo's downloads environment as AWS_ROLE_ARN.
set -euo pipefail
repo=$1
folder=$2
bucket=$3
role=github-${repo#*/}-downloads
export AWS_REGION=us-east-1
account=$(aws sts get-caller-identity --query Account --output text)

provider=arn:aws:iam::$account:oidc-provider/token.actions.githubusercontent.com
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$provider" >/dev/null 2>&1; then
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com --tags Key=Project,Value=downloads >/dev/null
fi

if ! aws iam get-role --role-name "$role" >/dev/null 2>&1; then
  aws iam create-role --role-name "$role" --tags Key=Project,Value=downloads \
    --description "$repo workflows publish to downloads.arkelectron.com/$folder" \
    --assume-role-policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Principal\": {\"Federated\": \"$provider\"},
    \"Action\": \"sts:AssumeRoleWithWebIdentity\",
    \"Condition\": {\"StringEquals\": {
      \"token.actions.githubusercontent.com:aud\": \"sts.amazonaws.com\",
      \"token.actions.githubusercontent.com:sub\": \"repo:$repo:environment:downloads\"
    }}
  }]
}" >/dev/null
fi

aws iam put-role-policy --role-name "$role" --policy-name publish --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::$bucket\"},
    {\"Effect\": \"Allow\", \"Action\": \"s3:GetObject\", \"Resource\": \"arn:aws:s3:::$bucket/*\"},
    {\"Effect\": \"Allow\", \"Action\": \"s3:PutObject\", \"Resource\": [
      \"arn:aws:s3:::$bucket/$folder/*\",
      \"arn:aws:s3:::$bucket/dev/$folder/*\",
      \"arn:aws:s3:::$bucket/index.html\",
      \"arn:aws:s3:::$bucket/*/index.html\",
      \"arn:aws:s3:::$bucket/manifest.json\"]},
    {\"Effect\": \"Allow\", \"Action\": \"s3:DeleteObject\", \"Resource\": \"arn:aws:s3:::$bucket/$folder/*/latest/*\"}
  ]
}"
aws iam get-role --role-name "$role" --query Role.Arn --output text
