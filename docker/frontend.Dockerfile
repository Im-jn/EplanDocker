FROM node:24-alpine AS build
WORKDIR /app
COPY pdf_reader/package.json pdf_reader/package-lock.json ./
RUN npm ci
COPY pdf_reader/ ./
ARG GIT_COMMIT=unknown
ENV VITE_GIT_COMMIT=$GIT_COMMIT
RUN npm run build

FROM nginx:1.28-alpine
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision=$GIT_COMMIT
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/healthz || exit 1
