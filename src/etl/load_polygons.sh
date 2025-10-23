#!/usr/bin/env bash
set -euo pipefail

TABLE="mml_building_polygon"

# 1️⃣ Get total feature count from WFS (using resultType=hits)
TOTAL=$(curl -s \
  "https://inspire-wfs.maanmittauslaitos.fi/inspire-wfs/bu_mtk_polygon?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=bu-core2d:Building&RESULTTYPE=hits" \
  | grep -o 'numberMatched="[0-9]*"' | grep -o '[0-9]*')
echo "Total features reported by WFS: $TOTAL"

# 2️⃣ Start ogr2ogr import in background (no TTY)
docker compose exec -T \
  -e OGR_WFS_PAGING_ALLOWED=YES \
  -e OGR_WFS_PAGE_SIZE=5000 \
  app \
  ogr2ogr \
    -skipfailures \
    -f PostgreSQL "PG:host=db port=5432 dbname=avoin_energy_map_db user=postgres password=postgres" \
    "WFS:https://inspire-wfs.maanmittauslaitos.fi/inspire-wfs/bu_mtk_polygon?SERVICE=WFS&VERSION=2.0.0" \
    -nln mml_building_polygon \
    -nlt MULTIPOLYGON \
    -sql "SELECT localId, elevationValue from \"bu-core2d:Building\"" \
    &
PID=$!

# 3️⃣ Wait until the table exists
until docker compose exec -T db psql -U postgres -d avoin_energy_map_db -Atc \
  "SELECT to_regclass('$TABLE')" | grep -q "$TABLE"; do
  echo "Waiting for $TABLE to be created..."
  sleep 5
done
echo "$TABLE exists, starting progress polling."

# 4️⃣ Poll row count and compute % progress
while kill -0 $PID 2>/dev/null; do
  COUNT=$(docker compose exec -T db psql -U postgres -d avoin_energy_map_db -Atc \
    "SELECT count(*) FROM $TABLE;" | tr -d '[:space:]')

  if [[ -n "${TOTAL}" && "${TOTAL}" -gt 0 ]]; then
    PERCENT=$(( 100 * COUNT / TOTAL ))
    echo "$(date +%H:%M:%S)  $COUNT / $TOTAL  (${PERCENT}%)"
  else
    echo "$(date +%H:%M:%S)  $COUNT rows imported"
  fi

  sleep 15
done

wait $PID
echo "ogr2ogr finished."

# 5️⃣ Final summary
COUNT=$(docker compose exec -T db psql -U postgres -d avoin_energy_map_db -Atc \
  "SELECT count(*) FROM $TABLE;" | tr -d '[:space:]')
echo "Final row count in $TABLE: $COUNT / $TOTAL"


# current status:
# ogr2ogr managed to run
# pagesize not working
# integer overflow in localid -> cast to char(20). 
# However the original localid is still imported, need to skip it