# Build context is the repo root, so Directory.Build.props is in scope.
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
WORKDIR /src

COPY Directory.Build.props ./
COPY src/Gielinomics.Client/Gielinomics.Client.csproj src/Gielinomics.Client/
COPY src/Gielinomics.Data/Gielinomics.Data.csproj src/Gielinomics.Data/
COPY src/Gielinomics.Ingest/Gielinomics.Ingest.csproj src/Gielinomics.Ingest/
RUN dotnet restore src/Gielinomics.Ingest/Gielinomics.Ingest.csproj

COPY src/ src/
RUN dotnet publish src/Gielinomics.Ingest/Gielinomics.Ingest.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/runtime:9.0
WORKDIR /app
COPY --from=build /app ./
ENTRYPOINT ["dotnet", "Gielinomics.Ingest.dll"]
