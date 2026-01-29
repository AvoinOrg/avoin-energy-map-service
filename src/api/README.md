# Running the API

1. Start the database server
```
make start
```
2. Start the api server
```
poetry run uvicorn src.api.main:app --reload
```
3. Run simple map api to debug the api at `src/debug/map.html`
Pass `env=sandbox` parameter (`src/debug/map.html?env=sandbox`) to load data from sandbox DB instead of local