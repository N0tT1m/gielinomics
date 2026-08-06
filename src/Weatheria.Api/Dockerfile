# Build context is the repo root, so Directory.Build.props is in scope.
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
WORKDIR /src

COPY Directory.Build.props ./
COPY src/Weatheria.Client/Weatheria.Client.csproj src/Weatheria.Client/
COPY src/Weatheria.Data/Weatheria.Data.csproj src/Weatheria.Data/
COPY src/Weatheria.Alerts/Weatheria.Alerts.csproj src/Weatheria.Alerts/
COPY src/Weatheria.Api/Weatheria.Api.csproj src/Weatheria.Api/
RUN dotnet restore src/Weatheria.Api/Weatheria.Api.csproj

COPY src/ src/
RUN dotnet publish src/Weatheria.Api/Weatheria.Api.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:9.0
WORKDIR /app
EXPOSE 8080
COPY --from=build /app ./
ENTRYPOINT ["dotnet", "Weatheria.Api.dll"]
