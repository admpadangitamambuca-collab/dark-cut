# Official Cobalt v11 image; no YouTube credentials in this repository.
FROM ghcr.io/imputnet/cobalt:11
USER root
COPY --chown=node:node cobalt/entrypoint.sh /app/darkcut-entrypoint.sh
USER node
CMD ["sh", "/app/darkcut-entrypoint.sh"]