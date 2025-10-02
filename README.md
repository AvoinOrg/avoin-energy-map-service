# Avoin energy map backend

This project consist of two parts
- ETL (Extract, transform, load) - Retrieve building data from the Finnish Environment Institute [https://paikkatiedot.ymparisto.fi](paikkatiedot.ymparisto.fi), orgainize and store in the project's own database
- API endpoints to return the building data based on the frontend's need.


## Tech stack
- Language: Python
- Package manager: Poetry
- Framework: Fast API
- Database: PostgreSQL with PostGIS for spatial data storage.
- Database Migrations: Alembic
- Containerization: Docker & Docker Compose

## Running in local
1. Copy the sample environment file
```
cp .env.template .env
```
2. Run the container
```
make start
```
3. Apply latest migration to local database
```
make migration_upgrade
```
4. Import codes and labels
```
make etl_load_codes
```
5. Load buildings. As there are ~4 million records in the original dataset, let's just test with a limited amount, before we figure out a way to optimize the data extraction mechanism. The limit number is the amount of records to pull to local DB.
```
make etl_load_codes_test limit=1000
```
Other commands can be found in the `makefile`
