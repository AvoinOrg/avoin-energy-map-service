# sync sandbox db with local container db dump
# run from root folder

set -a
source .env.sandbox
set +a

time pg_restore \
  --no-owner \
  --no-privileges \
  -h $PGHOST \
  -p $PGPORT \
  -U $PGUSER \
  -d $PGDATABASE \
  --clean --if-exists --jobs=8 db.dump