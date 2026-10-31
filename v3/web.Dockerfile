FROM nginx@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752
# Build v3/web/dist on the development machine before packaging this image.
COPY dist/ /usr/share/nginx/html/
EXPOSE 8080
