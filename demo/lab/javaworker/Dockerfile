# Java lab Worker. The SDK version is a build arg because the point of this lane
# is to show what a 1.2x Worker can and cannot do; see the comment in pom.xml.
#   docker compose --profile lab build --build-arg TEMPORAL_SDK_VERSION=1.29.0 lab-worker-java
FROM maven:3.9-eclipse-temurin-17 AS build
ARG TEMPORAL_SDK_VERSION=1.28.4
WORKDIR /src
COPY pom.xml ./
RUN mvn -q -B -Dtemporal.version=${TEMPORAL_SDK_VERSION} dependency:go-offline
COPY src ./src
# Tests run in the build: the flag's validation rules are the landmine list, and
# an image whose rules fail must not ship.
RUN mvn -q -B -Dtemporal.version=${TEMPORAL_SDK_VERSION} package

FROM eclipse-temurin:17-jre
RUN useradd -u 10001 app
COPY --from=build /src/target/lab-java-worker.jar /app/lab-java-worker.jar
USER app
# 9464 = SDK + JVM Prometheus metrics
EXPOSE 9464
# -Xmx is explicit: ResourceBasedTuner measures memory as JVM heap used / -Xmx,
# not container memory. Leave headroom under the container limit for thread
# stacks (one platform thread per Activity slot and per cached Workflow),
# metaspace and gRPC's direct buffers, none of which the tuner sees.
ENV JAVA_OPTS="-Xmx384m -XX:+ExitOnOutOfMemoryError"
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/lab-java-worker.jar"]
