# Pinned to a patch release, not the floating 8.3 tag: B05 builds once per
# VERSION and reuses the image, and a cached php:8.3-fpm-alpine on the host is
# reused by docker build. 8.3.36 carries the fixes for CVE-2026-109618 and
# CVE-2026-109619 (unauthenticated RCE). Raise the pin, and VERSION with it,
# for later PHP security releases.
FROM php:8.3.36-fpm-alpine

# Install nginx and required extensions
RUN apk add --no-cache nginx supervisor

# Configure PHP
# opcache commented out 2026-05-27: docker-php-ext-install runs phpize + ./configure
# + make inside the image and accounts for ~5 of the 8:32 B05 wall time.
# Authum is a low-traffic admin auth panel; the perf gain isn't worth the install cost
# at this stage. Re-enable here (or move to a pre-built smart-node-authum image / golden
# image, use a separate image stage if profiling shows a real bottleneck.
# RUN docker-php-ext-install opcache

# Copy nginx config
COPY nginx.conf /etc/nginx/http.d/default.conf

# Copy supervisord config
COPY supervisord.conf /etc/supervisord.conf

# Create app directory
WORKDIR /app

# Copy application files
COPY --chmod=644 src/ /app/

# Create data directories with proper permissions
RUN mkdir -p /app/auth /app/data/ratelimit && \
    chown -R nobody:nobody /app && \
    chmod -R 755 /app/data

EXPOSE 80

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
