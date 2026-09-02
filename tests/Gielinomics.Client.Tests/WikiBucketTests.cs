using System.Net;
using System.Text.Json;
using Gielinomics.Client.Json;
using Gielinomics.Client.Wiki;
using Xunit;

namespace Gielinomics.Client.Tests;

/// <summary>
/// The Bucket query builder. The rendered string is Lua the wiki evaluates, so its shape is
/// the contract.
/// </summary>
public class BucketQueryTests
{
    [Fact]
    public void Renders_the_documented_call_chain()
    {
        var query = new BucketQuery("infobox_item")
            .Select("item_id", "image", "examine")
            .Where("item_name", "Raw lobster");

        Assert.Equal(
            "bucket('infobox_item').select('item_id','image','examine').where('item_name','Raw lobster').run()",
            query.ToString());
    }

    [Fact]
    public void Escapes_an_apostrophe_in_a_value()
    {
        // The value is interpolated into evaluated Lua, and hundreds of item names carry an
        // apostrophe. Unescaped, this terminates the literal.
        var query = new BucketQuery("infobox_item").Select("item_name").Where("item_name", "Ava's accumulator");

        Assert.Contains(@"'Ava\'s accumulator'", query.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Escapes_a_backslash_before_quoting()
    {
        var rendered = BucketQuery.Quote(@"a\b");

        // The backslash is doubled first, so it cannot escape the quote that follows it.
        Assert.Equal(@"'a\\b'", rendered);
    }

    [Fact]
    public void OrderBy_adds_the_field_to_the_projection()
    {
        // The API rejects ordering by a field that is not selected. Discovering that during a
        // weekly sync rather than here would cost a week of stale data.
        var query = new BucketQuery("dropsline").Select("drop_json").OrderBy("item_name");

        Assert.Contains("select('drop_json','item_name')", query.ToString(), StringComparison.Ordinal);
        Assert.Contains("orderBy('item_name')", query.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void OrderBy_does_not_duplicate_an_already_selected_field()
    {
        var query = new BucketQuery("x").Select("a", "b").OrderBy("a");

        Assert.Contains("select('a','b')", query.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Limit_and_offset_render_in_order()
    {
        var query = new BucketQuery("x").Select("a").Limit(5000).Offset(10000);

        Assert.EndsWith(".limit(5000).offset(10000).run()", query.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void A_query_with_no_projection_is_rejected()
        => Assert.Throws<InvalidOperationException>(() => new BucketQuery("x").ToString());
}

/// <summary>Rarity parsing, against the shapes the live drop table actually contains.</summary>
public class DropRarityTests
{
    [Theory]
    [InlineData("Always", 1.0)]
    [InlineData("always", 1.0)]
    [InlineData("1/1", 1.0)]
    [InlineData("1/512", 1.0 / 512)]
    [InlineData("12/128", 12.0 / 128)]
    [InlineData("1/2", 0.5)]
    public void Parses_the_numeric_forms(string text, double expected)
    {
        var parsed = DropRarity.Parse(text);

        Assert.NotNull(parsed);
        Assert.Equal(expected, (double)parsed.Value, 10);
    }

    [Fact]
    public void Parses_a_denominator_with_a_separator_and_a_decimal()
    {
        // '1/5,461.33' is the single most common non-trivial form in the live table, and the
        // obvious regex for "n/m" does not match it.
        var parsed = DropRarity.Parse("1/5,461.33");

        Assert.NotNull(parsed);
        Assert.Equal(1d / 5461.33, (double)parsed.Value, 10);
    }

    [Theory]
    [InlineData("Varies")]
    [InlineData("Unknown")]
    [InlineData("Rare")]
    [InlineData("Common")]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    public void Refuses_to_invent_a_probability_for_qualitative_text(string? text)
    {
        // Several hundred rows say things like this. A guessed number would propagate straight
        // into an expected-gp figure somebody acts on, so null is the only honest answer.
        Assert.Null(DropRarity.Parse(text));
    }

    [Theory]
    [InlineData("1/0")]
    [InlineData("2/1")]
    [InlineData("-1/5")]
    [InlineData("5")]
    public void Rejects_values_that_are_not_probabilities(string text)
        => Assert.Null(DropRarity.Parse(text));
}

/// <summary>Bucket's boolean encoding, which is not booleans.</summary>
public class BucketFlagsTests
{
    [Fact]
    public void An_empty_string_means_the_flag_is_set()
    {
        // Bucket sends a set flag as "" and omits an unset one entirely. Modelling these as
        // bool? is the obvious thing and fails to parse every row that has one.
        Assert.True(BucketFlags.IsSet(string.Empty));
    }

    [Fact]
    public void An_absent_field_means_the_flag_is_not_set()
        => Assert.False(BucketFlags.IsSet(null));

    [Theory]
    [InlineData("true")]
    [InlineData("1")]
    [InlineData("yes")]
    public void A_present_value_is_also_set(string value)
        => Assert.True(BucketFlags.IsSet(value));

    [Theory]
    [InlineData("false")]
    [InlineData("0")]
    public void An_explicit_negative_is_not_set(string value)
        => Assert.False(BucketFlags.IsSet(value));
}

/// <summary>The client, against recorded Bucket responses.</summary>
public class WikiBucketClientTests
{
    private static WikiBucketClient Client(FixtureHandler handler)
    {
        var http = new HttpClient(handler, disposeHandler: false)
        {
            BaseAddress = new Uri("https://oldschool.runescape.wiki/"),
        };
        http.DefaultRequestHeaders.UserAgent.ParseAdd("gielinomics-tests/0.1");
        return new WikiBucketClient(http);
    }

    [Fact]
    public async Task Decodes_rows_and_their_repeated_fields()
    {
        using var handler = FixtureHandler.FromBody(
            """
            {"bucketQuery":"...","bucket":[
              {"page_name":"Abyssal whip","item_name":"Abyssal whip","item_id":["4151"],"is_members_only":"","buy_limit":70}
            ]}
            """);

        var rows = await Client(handler).QueryAsync<BucketItem>(
            new BucketQuery("infobox_item").Select("page_name"), CancellationToken.None);

        var row = Assert.Single(rows);
        Assert.Equal("Abyssal whip", row.PageName);
        Assert.Equal(["4151"], row.ItemId);
        Assert.True(BucketFlags.IsSet(row.IsMembersOnly));
        Assert.Equal(70, row.BuyLimit);
    }

    [Fact]
    public async Task A_query_error_is_raised_even_though_the_status_is_200()
    {
        // Bucket reports a bad query with HTTP 200 and an error field. A status check alone
        // would read a Lua syntax error as a successful sync that found nothing — and the
        // full-replace load would then empty the table.
        using var handler = FixtureHandler.FromBody(
            """{"bucketQuery":"...","error":"Bucket no_such_bucket does not exist."}""");

        var ex = await Assert.ThrowsAsync<WikiApiException>(
            () => Client(handler).QueryAsync<BucketItem>(new BucketQuery("x").Select("a"), CancellationToken.None));

        Assert.Contains("does not exist", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_failing_status_is_raised()
    {
        using var handler = FixtureHandler.FromBody("nope", HttpStatusCode.BadGateway, "text/plain");

        var ex = await Assert.ThrowsAsync<WikiApiException>(
            () => Client(handler).QueryAsync<BucketItem>(new BucketQuery("x").Select("a"), CancellationToken.None));

        Assert.Equal(HttpStatusCode.BadGateway, ex.StatusCode);
    }

    [Fact]
    public async Task An_empty_result_is_an_empty_list_not_a_null()
    {
        using var handler = FixtureHandler.FromBody("""{"bucketQuery":"...","bucket":[]}""");

        Assert.Empty(await Client(handler).QueryAsync<BucketItem>(
            new BucketQuery("x").Select("a"), CancellationToken.None));
    }

    [Fact]
    public async Task StreamAsync_stops_on_a_short_page()
    {
        // A short page is the end of the bucket. Asking for one more costs a request per sync
        // to learn nothing.
        using var handler = FixtureHandler.FromBody(
            """{"bucketQuery":"...","bucket":[{"page_name":"a"},{"page_name":"b"}]}""");

        var rows = new List<BucketBonuses>();
        await foreach (var row in Client(handler).StreamAsync<BucketBonuses>(
            () => new BucketQuery("infobox_bonuses").Select("page_name"), "page_name", pageSize: 5))
        {
            rows.Add(row);
        }

        Assert.Equal(2, rows.Count);
        Assert.Equal(1, handler.RequestCount);
    }

    [Fact]
    public void DropJson_decodes_into_the_fields_the_drop_table_needs()
    {
        const string Raw = """
            {"Rarity":"1/512","Drop type":"combat","Dropped from":"Abyssal demon#Standard",
             "Quantity High":1,"Rolls":1,"Quantity Low":1,"Dropped item":"Abyssal whip"}
            """;

        var detail = JsonSerializer.Deserialize(Raw, GielinomicsJsonContext.Default.DropDetail);

        Assert.NotNull(detail);
        Assert.Equal("Abyssal demon#Standard", detail.DroppedFrom);
        Assert.Equal("Abyssal whip", detail.DroppedItem);
        Assert.Equal(1, detail.Rolls);
        Assert.Equal(1m / 512m, DropRarity.Parse(detail.Rarity));
    }
}
