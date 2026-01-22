# run from root folder

set -a
source .env
set +a

time pg_dump -h localhost -U $PGUSER -d $PGDATABASE \
  -Fc -f db.dump -t codes_maps -t buildings -t addresses -t polygons