using System.Text.Json;
using System.Text.Json.Serialization;
using Gielinomics.Client.Hiscores;
using Gielinomics.Client.Prices;
using Gielinomics.Client.Wiki;
using Gielinomics.Client.WiseOldMan;

namespace Gielinomics.Client.Json;

/// <summary>
/// Source-generated serialisation contexts for every wire type in this package.
/// </summary>
/// <remarks>
/// Source generation rather than reflection: the ingest worker deserialises a ~3700-entry
/// map every 60 seconds, and this keeps that allocation-light and trim/AOT-safe.
/// </remarks>
[JsonSourceGenerationOptions(
    PropertyNameCaseInsensitive = true,
    NumberHandling = JsonNumberHandling.AllowReadingFromString)]
[JsonSerializable(typeof(PriceEnvelope<LatestPrice>), TypeInfoPropertyName = "PriceEnvelopeLatestPrice")]
[JsonSerializable(typeof(PriceEnvelope<PriceBar>), TypeInfoPropertyName = "PriceEnvelopePriceBar")]
[JsonSerializable(typeof(IReadOnlyList<ItemMapping>), TypeInfoPropertyName = "IReadOnlyListItemMapping")]
[JsonSerializable(typeof(TimeSeriesResponse))]
[JsonSerializable(typeof(HiscoreProfile))]
[JsonSerializable(typeof(BucketEnvelope<BucketItem>), TypeInfoPropertyName = "BucketEnvelopeBucketItem")]
[JsonSerializable(typeof(BucketEnvelope<BucketBonuses>), TypeInfoPropertyName = "BucketEnvelopeBucketBonuses")]
[JsonSerializable(typeof(BucketEnvelope<BucketDrop>), TypeInfoPropertyName = "BucketEnvelopeBucketDrop")]
[JsonSerializable(typeof(BucketEnvelope<BucketMonster>), TypeInfoPropertyName = "BucketEnvelopeBucketMonster")]
[JsonSerializable(typeof(DropDetail))]
[JsonSerializable(typeof(WiseOldManPlayer))]
[JsonSerializable(typeof(WiseOldManGains))]
[JsonSerializable(typeof(IReadOnlyList<WiseOldManSnapshot>), TypeInfoPropertyName = "IReadOnlyListWiseOldManSnapshot")]
[JsonSerializable(typeof(WiseOldManGroup))]
[JsonSerializable(typeof(IReadOnlyList<WiseOldManGroupGains>), TypeInfoPropertyName = "IReadOnlyListWiseOldManGroupGains")]
[JsonSerializable(typeof(WiseOldManCompetition))]
[JsonSerializable(typeof(IReadOnlyList<WiseOldManEfficiencyRate>), TypeInfoPropertyName = "IReadOnlyListWiseOldManEfficiencyRate")]
[JsonSerializable(typeof(WiseOldManError))]
public sealed partial class GielinomicsJsonContext : JsonSerializerContext
{
}
