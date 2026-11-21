FROM debian:13-slim AS build
WORKDIR /src
COPY app/ app/
COPY packaging/ packaging/
ARG VERSION
RUN sh packaging/build-deb.sh "${VERSION}" /out

FROM scratch
COPY --from=build /out/ /
