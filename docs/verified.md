# Verified facts

Behaviour of the APIs this toolkit depends on, checked against live calls
rather than taken from documentation. Each row gives the evidence and the
date. Facts that are only documented, not exercised, are marked as such.

## Magic Eden public API (2026-10-27, live calls, no key)

| Fact | Evidence |
| --- | --- |
| `limit` must be within [1, 500] on activities, pools and wallet tokens; **listings accept at most 100** | `?limit=1000` returns 400 `limit should be within [1, 500]`; `/listings?limit=101` returns 400 while `limit=100` returns 200 |
| `offset` is rejected above ~15,500 | `offset=15000` returns a full page; `offset=16000` and above return 400 `offset should be non-negative integer` |
| `type` filter is a comma-delimited list and each filtered query gets its own offset budget | `?type=buyNow` returns only buyNow; `type[]=` is rejected with the list of valid values: `buyNow, buy, list, delist, bid, cancelBid, auctionCreated, auctionUpdated, auctionCanceled, auctionSettled, auctionPlaceBid, poolUpdate, mint, transfer` |
| History depth is about 90 days, not since mint | For the test collection, buyNow, list and delist all stopped ~90 days before the pull; `/v2/tokens/{mint}/activities` for a mint sold on the cut-off day showed nothing earlier either. `bid`/`cancelBid` (M2 offers) reached back further. **Early history is not available from this API.** |
| `type=bid` returns M2 offers only; pool bids appear as `type: bid, source: mmm` in the unfiltered feed | The unfiltered feed holds thousands of `bid` rows with `source: mmm` in the same hours as the `poolUpdate` rows. Pool-side events carry the pool owner in `seller`, not `buyer`. |
| An unfiltered feed can be dominated by pool-update spam | 15,500 unfiltered rows spanned ~28 hours for an actively market-made collection; `type=poolUpdate` alone spanned ~17 days at the offset cap. Sales must be pulled with `type=buyNow`. |
| Pool fills appear as `buyNow` with `source: mmm`; listing buys as `source: magiceden_v2` | observed on every sale in the window |
| `price` in activities is SOL as a float; `priceInfo.solPrice.rawAmount` is not lamports for mmm rows (off by 1e9) | bid row: `price: 50.201`, `rawAmount: 50201000000000000000` |
| Pools endpoint shape | `GET /mmm/pools?collectionSymbol=` returns `{results: [...]}` with `spotPrice`, `buysidePaymentAmount` (lamports), `poolType` (`buy_sided` / `sell_sided` / `two_sided`), `sharedEscrowInfo.sharedEscrowBuysidePaymentAmount`, `cosigner`, `buysideCreatorRoyaltyBp`, `collectionSellerFeeBasisPoints`, `isMIP1`, `isOCP`. `field=5&direction=1` sorts by net bid. |
| Live pools do **not** carry `buyPriceTaker` although the schema lists it | none of 46 pools had the field |
| Listings endpoint shape | `GET /collections/{symbol}/listings` rows carry `pdaAddress`, `tokenMint`, `tokenAddress`, `seller`, `price` (SOL), `expiry` (-1 = none), `listingSource: M2`, `auctionHouse: E8cU1WiRWjanGxmn96ewBgk9vPTcL6AEZ1t6F6fkgUWe` |
| `GET /collections/{symbol}` (detail) returns an empty body | names come from `collectionName` on listings/pools instead |
| Docs say `/activities` accepts `limit` up to 1000; the live API caps it at 500 | OpenAPI on the docs page says "max 1000"; the live call returns 400 above 500. **Trust the live API for limits.** |
| Public rate limit | 120 requests per minute / 2 QPS, per the API keys page; the client here runs at 100 per minute |

## Magic Eden instruction endpoints (docs.magiceden.io OpenAPI, pages dated 2025-09-12, read 2026-10-27)

Parameter names and required flags from the OpenAPI definitions. **Not yet
exercised against the live API**; that needs an API key.

Common to all: base `https://api-mainnet.magiceden.dev/v2` (devnet:
`api-devnet.magiceden.dev/v2`), `GET`, header `Authorization: Bearer <key>`.
Response is `{ tx: {type: "Buffer", data: number[]}, txSigned?: {...} }`; the
official recipe deserialises `txSigned.data` with `VersionedTransaction.deserialize`,
signs with the seller keypair, sends through its own RPC and confirms with the
`blockhashData.blockhash` / `lastValidBlockHeight` the response also carries.
Prices are in **SOL as decimals**, not lamports. Priority fee can be passed as
`priorityFee` (microlamports) or `prioFeeMicroLamports` / `maxPrioFeeLamports` /
`exactPrioFeeLamports`. `auctionHouseAddress` is optional and defaults to the
main auction house, `E8cU1WiRWjanGxmn96ewBgk9vPTcL6AEZ1t6F6fkgUWe`.

| Purpose | Path | Required query params | Optional |
| --- | --- | --- | --- |
| List | `/instructions/sell` | `seller`, `tokenMint`, `tokenAccount`, `price` | `auctionHouseAddress`, `sellerReferral`, `expiry` (unix seconds, 0 = none), priority-fee params |
| Change listing price | `/instructions/sell_change_price` | `seller`, `tokenMint`, `tokenAccount`, `price` (current), `newPrice`, `expiry` (**required** here) | `auctionHouseAddress`, `sellerReferral`, `priorityFee` |
| Cancel listing | `/instructions/sell_cancel` | `seller`, `tokenMint`, `tokenAccount`, `price` (current) | `auctionHouseAddress`, `sellerReferral`, `expiry`, priority-fee params |
| Accept an M2 offer | `/instructions/sell_now` | `buyer`, `seller`, `tokenMint`, `tokenATA`, `price`, `newPrice`, `sellerExpiry` | `auctionHouseAddress`, referrals, `buyerExpiry`, priority-fee params |
| Sell into a pool bid | `/instructions/mmm/sol-fulfill-buy` | `pool`, `assetAmount`, `minPaymentAmount` (SOL), `seller`, `assetMint`, `assetTokenAccount` | `allowlistAuxAccount`, `skipDelist`, `priorityFee` |
| Buy a listing | `/instructions/buy_now` | `buyer`, `seller`, `tokenMint`, `tokenATA`, `price`, `sellerExpiry` | `auctionHouseAddress`, referrals, `buyerExpiry`, `buyerCreatorRoyaltyPercent`, priority-fee params, `splPrice` |

Design notes that follow from this:

- `sell_change_price` needs the **current** `price` and an `expiry`, so the
  reconciler must keep the live listing price and expiry read back from
  `/collections/{symbol}/listings`.
- `sol-fulfill-buy` carries `minPaymentAmount`, the natural place for the
  reservation price and the take-bid band: the transaction fails instead of
  filling if the pool's spot moved below it between the read and the send.
- `sol-fulfill-buy` auto-delists a listed NFT unless `skipDelist=true`.

## Magic Eden AMM (MMM) pools

| Fact | Evidence |
| --- | --- |
| Effective price when a pool buys (collection offer): `e = s0 × (1 − r·f − l − t)`; when a pool sells: `e = s−1 × (1 + r·f + l + t)`, with royalty share `r`, seller fee bps `f`, LP fee `l` (0 for one-sided pools), taker fee `t` | docs.magiceden.io `reference/mmm`, read 2026-10-27. **The numeric taker fee is not on the page**; it is set in the transactions Magic Eden cosigns and must be read from a real fill. |
| Every pool instruction, including the buyer's purchase from a sell-side pool and the seller's fill of a bid, requires the pool's **cosigner** to sign, and the cosigner must differ from the owner | `me-foundation/mmm` program source: `cosigner: Signer` with `constraint = owner.key() != cosigner.key()` and `has_one = cosigner` on `create_pool`, `update_pool`, `deposit_sell`, `withdraw_sell`, `sol_fulfill_buy`, `sol_fulfill_sell`. Magic Eden's pools use Magic Eden's cosigner. **Consequence: pools cannot be created, repriced or filled on Magic Eden without their UI or their API key; a pool created elsewhere cannot be bought from on their site.** |
| Magic Eden fee on all transactions is 2% | help centre "What fees will I pay to list or sell NFTs"; who bears it on a pool fill is read from the first real transaction |
| Pool UI: the "Start Sell Price" / "Start Buy Price" fields are "Inclusive of all fees" | collection page → Pools → Create Pool, 2026-10-27 |

## Price data sources (2026-10-27)

| Fact | Evidence |
| --- | --- |
| CoinGecko public tier limits `market_chart` to 365 days | `days=max` returns 401 error 10012; `days=365` works |
| CoinGecko NFT `market_chart` needs a key; the NFT detail endpoint does not | `/nfts/{id}/market_chart?days=365` returns 401; `/nfts/{id}` returns floor, 24h sales, owners |
| Coinbase Exchange gives full daily SOL-EUR and SOL-USD history without a key | `/products/SOL-EUR/candles?granularity=86400` returns 300 candles per call back to at least 2022-06 |
| Kraken public OHLC covers the last 720 days only | `SOLEUR` interval 1440 returns 721 rows |
