#!/usr/bin/env bash
# Export every published contact flow to disk, so the repository is the
# version history.
#
# Why this script exists: CreateContactFlowVersion only supports flows of
# type CAMPAIGN, so there is NO API version history for ordinary flows and
# no rollback to "version 3". Git holding the exported JSON is the rollback
# mechanism. That is not a workaround, it is the documented position.
set -euo pipefail

: "${INSTANCE_ID:?export INSTANCE_ID first}"
OUT="${1:-flows}"
mkdir -p "$OUT"

aws connect list-contact-flows --instance-id "$INSTANCE_ID" \
  --query 'ContactFlowSummaryList[].[Id,Name]' --output text \
| while read -r ID NAME; do
    aws connect describe-contact-flow \
      --instance-id "$INSTANCE_ID" --contact-flow-id "$ID" \
      --query 'ContactFlow.Content' --output text \
    | python3 -m json.tool > "${OUT}/${NAME}.json"
    echo "exported ${OUT}/${NAME}.json"
  done

echo
echo "Checking for unpublished edits left in the designer..."
aws connect list-contact-flows --instance-id "$INSTANCE_ID" \
  --query 'ContactFlowSummaryList[].[Id,Name]' --output text \
| while read -r ID NAME; do
    LIVE=$(aws connect describe-contact-flow --instance-id "$INSTANCE_ID" \
             --contact-flow-id "$ID" --query 'ContactFlow.Content' \
             --output text 2>/dev/null || echo "")
    SAVED=$(aws connect describe-contact-flow --instance-id "$INSTANCE_ID" \
             --contact-flow-id "${ID}:\$SAVED" --query 'ContactFlow.Content' \
             --output text 2>/dev/null || echo "$LIVE")
    if [ "$LIVE" != "$SAVED" ]; then
      echo "  DRIFT: ${NAME} has saved content that was never published"
    fi
  done
echo "done"
