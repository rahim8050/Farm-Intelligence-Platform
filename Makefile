.PHONY: help up up-full down logs shell migrate createsuperuser worker beat

help:
	@printf '%s\n' \
		'Available targets:' \
		'  make up              Start db, redis, migrate, and web' \
		'  make up-full         Start the full stack with worker and beat' \
		'  make down            Stop the stack and remove volumes' \
		'  make logs            Follow web, worker, and beat logs' \
		'  make shell           Open a shell in the web container' \
		'  make migrate         Run database migrations' \
		'  make createsuperuser Create a Django superuser' \
		'  make worker          Start Celery worker (profile)' \
		'  make beat            Start Celery beat (profile)'

up:
	docker compose up --build

up-full:
	docker compose --profile worker --profile beat up --build

down:
	docker compose down -v

logs:
	docker compose logs -f web worker beat

shell:
	docker compose exec web sh

migrate:
	docker compose exec web python manage.py migrate --noinput

createsuperuser:
	docker compose exec web python manage.py createsuperuser

worker:
	docker compose --profile worker up --build worker

beat:
	docker compose --profile beat up --build beat
