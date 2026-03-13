# Install the project

apt install postgresql-18-pgvector postgresql-18 postgresql-client-18 build-essential python3-dev

# Create database

connect to postgres
```
sudo -u postgres psql
```

create empty db
```
CREATE DATABASE <my_db_name>;
...
\q
```

install current db structure
```
make db
```

create .env and modify it by your creds
```
cp .env.example .env
nano .env
```

create file local.yaml in root folder of project

```
make run
```

to run project

# Deploy

To deploy all parts of the project (including Telegram workers), the tag name must end with 't'. Otherwise, the Telegram component will remain unchanged.
