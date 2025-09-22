Start container

`docker compose up`

Running commands in container

`docker exec -it energy_app bash`

Inspect database

`docker compose exec db psql -U ${PGUSER:-postgres} -d ${PGDATABASE:-avoin_energy_map_db}`