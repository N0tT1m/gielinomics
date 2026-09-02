using System.ComponentModel.DataAnnotations;

namespace Gielinomics.Client;

/// <summary>Configuration for the Gielinomics API clients.</summary>
public sealed class GielinomicsClientOptions
{
    /// <summary>The prices API base address. Note this is <b>v2</b>; v1 still responds but is legacy.</summary>
    public Uri PricesBaseAddress { get; set; } = new("https://prices.runescape.wiki/api/v2/osrs/");

    /// <summary>The official hiscores base address.</summary>
    public Uri HiscoresBaseAddress { get; set; } = new("https://secure.runescape.com/");

    /// <summary>
    /// The OSRS wiki base address, which serves the Bucket structured-data API.
    /// </summary>
    /// <remarks>
    /// A different host from the prices API, despite both being "the wiki" — prices live on
    /// <c>prices.runescape.wiki</c>, structured data on the wiki proper.
    /// </remarks>
    public Uri WikiBaseAddress { get; set; } = new("https://oldschool.runescape.wiki/");

    /// <summary>The Wise Old Man v2 base address.</summary>
    public Uri WiseOldManBaseAddress { get; set; } = new("https://api.wiseoldman.net/v2/");

    /// <summary>
    /// A descriptive User-Agent identifying you and giving the wiki a way to contact you.
    /// </summary>
    /// <remarks>
    /// Required, not advisory. The wiki blocks a list of default agents outright, so an
    /// unset value means every request fails. Include a contact URL, e.g.
    /// <c>gielinomics/0.1 (github.com/N0tT1m/gielinomics)</c>.
    /// </remarks>
    [Required(AllowEmptyStrings = false, ErrorMessage =
        "UserAgent is required. The OSRS wiki blocks default agents; set a descriptive one with a contact URL.")]
    public string UserAgent { get; set; } = string.Empty;

    /// <summary>Optional Wise Old Man API key, sent as <c>x-api-key</c>.</summary>
    public string? WiseOldManApiKey { get; set; }

    /// <summary>Per-request timeout.</summary>
    public TimeSpan Timeout { get; set; } = TimeSpan.FromSeconds(30);
}
