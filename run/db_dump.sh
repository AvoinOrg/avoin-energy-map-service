# run from root folder

set -a
source .env
set +a

time pg_dump -h localhost -U $PGUSER -d $PGDATABASE \
  -v \
  -Fc -f db.dump -t codes_maps -t main