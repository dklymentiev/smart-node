# Smart Node UI — static control panel (production image).
# Static panel served by nginx; /api/* is proxied to the core-api backend.
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY src/ /usr/share/nginx/html/
# The footer reads the panel version from /version.txt (shell.js), not from a
# string hardcoded in every page.
COPY VERSION /usr/share/nginx/html/version.txt

# /data is populated at runtime by sn-panel-data.sh in production.
# Pre-create so nginx doesn't error before snapshots exist.
RUN mkdir -p /usr/share/nginx/html/data

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
