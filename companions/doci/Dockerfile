FROM php:8.2-apache

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install PHP extensions
RUN docker-php-ext-install pdo_pgsql

# Enable Apache modules
RUN a2enmod rewrite headers

# Copy Apache configuration
COPY apache-config.conf /etc/apache2/conf-available/custom.conf
RUN a2enconf custom

# Configure PHP for production
RUN mv "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini"

# Create log directory
RUN mkdir -p /var/log/doci && chown www-data:www-data /var/log/doci

# Set working directory
WORKDIR /var/www/html

# Copy application files
COPY --chown=www-data:www-data . .

# Configure git for root (for CLI operations)
RUN git config --global --add safe.directory /var/www/html \
    && git config --global --add safe.directory /var/www/html/files \
    && git config --global user.email "doci@example.com" \
    && git config --global user.name "DOCI"

# Configure git for www-data (for web operations)
RUN mkdir -p /var/www/.config/git \
    && echo "[safe]" > /var/www/.config/git/config \
    && echo "    directory = /var/www/html" >> /var/www/.config/git/config \
    && echo "    directory = /var/www/html/files" >> /var/www/.config/git/config \
    && chown -R www-data:www-data /var/www/.config

# Custom entrypoint: init files/ as a git repo on first run, then hand off
# to the default php:8.2-apache entrypoint.
RUN chmod +x /var/www/html/scripts/docker-entrypoint.sh
ENTRYPOINT ["/var/www/html/scripts/docker-entrypoint.sh"]
CMD ["apache2-foreground"]

# Expose port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost/api/health.php || exit 1
