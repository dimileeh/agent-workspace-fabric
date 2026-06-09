FROM node:22-alpine

WORKDIR /app

COPY package.json ./package.json
COPY server.mjs ./server.mjs
COPY scripts/container-healthcheck.mjs ./scripts/container-healthcheck.mjs

ENV NODE_ENV=production
ENV PORT=3000

EXPOSE 3000

CMD ["node", "/app/server.mjs"]
