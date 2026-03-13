deploy:
	@if [ -f package-lock.json ]; then \
		npm ci || npm install; \
	else \
		npm install; \
	fi
	npm run build
