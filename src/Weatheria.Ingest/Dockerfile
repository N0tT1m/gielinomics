# Build context is the repo root, so Directory.Build.props is in scope.
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
WORKDIR /src

COPY Directory.Build.props ./
COPY src/Weatheria.Client/Weatheria.Client.csproj src/Weatheria.Client/
COPY src/Weatheria.Data/Weatheria.Data.csproj src/Weatheria.Data/
COPY src/Weatheria.Ingest/Weatheria.Ingest.csproj src/Weatheria.Ingest/
RUN dotnet restore src/Weatheria.Ingest/Weatheria.Ingest.csproj

COPY src/ src/
RUN dotnet publish src/Weatheria.Ingest/Weatheria.Ingest.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/runtime:9.0
WORKDIR /app
COPY --from=build /app ./
ENTRYPOINT ["dotnet", "Weatheria.Ingest.dll"]
