FROM debian:13-slim AS build
WORKDIR /src
COPY app/ app/
COPY packaging/ packaging/
ARG VERSION
RUN sh packaging/build-deb.sh "${VERSION}" /out

FROM debian:13-slim AS dev
COPY --from=build /out/ /tmp/packages/
RUN apt-get update \
    && apt-get install -y --no-install-recommends /tmp/packages/*.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/packages
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["soundwave"]

FROM scratch
COPY --from=build /out/ /
