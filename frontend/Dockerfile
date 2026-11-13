# --- build stage ---
FROM node:22-alpine AS build
WORKDIR /app
ARG NPM_REGISTRY=
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund ${NPM_REGISTRY:+--registry=$NPM_REGISTRY}
COPY . .
RUN npm run build

# --- serve stage ---
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
