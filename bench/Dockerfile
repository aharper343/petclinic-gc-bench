# Benchmark image for spring-petclinic-rest — used to test JDK 25 flag
# combinations. JVM flags are supplied at `docker run` time via JDK_JAVA_OPTIONS,
# not baked into this image, so one image serves every combo in the matrix.
#
# Build from the repository root: the source is upstream spring-petclinic-rest
# (the app/ submodule) with the files under bench/ copied over it.
#   docker build -f bench/Dockerfile -t petclinic-bench:jdk25-v2 .
FROM maven:3.9-eclipse-temurin-25 AS build
WORKDIR /src
COPY app/pom.xml .
RUN mvn -B -q dependency:go-offline || true
COPY app/src ./src
COPY bench/src ./src
RUN mvn -B -q -DskipTests package

FROM eclipse-temurin:25-jdk
WORKDIR /app
COPY --from=build /src/target/*.jar app.jar
# Quiet down per-query SQL logging and the H2 startup banner noise so they
# don't skew CPU/latency measurements; profile stays h2 + spring-data-jpa
# (in-memory DB, no separate DB container needed).
ENV SPRING_PROFILES_ACTIVE=h2,spring-data-jpa
ENV SPRING_JPA_SHOW_SQL=false
ENV SPRING_JPA_HIBERNATE_DDL_AUTO=update
EXPOSE 9966
# JDK_JAVA_OPTIONS is read natively by the `java` launcher (JDK 9+) — pass
# the flag combo at `docker run -e JDK_JAVA_OPTIONS="..."` time. Using the
# exec-form entrypoint below (no shell) avoids re-parsing/double-applying it.
ENTRYPOINT ["java", "-jar", "app.jar"]
