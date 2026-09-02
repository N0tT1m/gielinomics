# Build context is the repo root, so Directory.Build.props is in scope.
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
WORKDIR /src

COPY Directory.Build.props ./
COPY src/Gielinomics.Client/Gielinomics.Client.csproj src/Gielinomics.Client/
COPY src/Gielinomics.Data/Gielinomics.Data.csproj src/Gielinomics.Data/
COPY src/Gielinomics.Alerts/Gielinomics.Alerts.csproj src/Gielinomics.Alerts/
COPY src/Gielinomics.Ingest/Gielinomics.Ingest.csproj src/Gielinomics.Ingest/
RUN dotnet restore src/Gielinomics.Ingest/Gielinomics.Ingest.csproj

COPY src/ src/
RUN dotnet publish src/Gielinomics.Ingest/Gielinomics.Ingest.csproj -c Release -o /app

# aspnet, not runtime: the worker hosts a Prometheus scrape endpoint alongside its
# BackgroundServices, so Grafana can alert on staleness without anything else existing.
FROM mcr.microsoft.com/dotnet/aspnet:9.0
WORKDIR /app
EXPOSE 8080
COPY --from=build /app ./
ENTRYPOINT ["dotnet", "Gielinomics.Ingest.dll"]
