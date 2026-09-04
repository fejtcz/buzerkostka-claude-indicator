# Why your cube might show green when you ask for red

Short version: run `buzerkostka calibrate` and forget about it. The long
version is below, because the cause is genuinely confusing and it is worth
knowing before you file a bug against the wrong project.

## The firmware quirk

The stock [BuzerKostka firmware](https://github.com/Pajenicko/BuzerKostka)
initialises FastLED with the LED declared as `GRB`:

```cpp
FastLED.addLeds<WS2812, LED_PIN, GRB>(leds, NUM_LEDS);
```

That tells FastLED to emit each pixel as `[pixel.g, pixel.r, pixel.b]`,
which is exactly what a GRB-ordered WS2812 expects. So far, so normal.

But when a colour arrives over MQTT the firmware assigns it *pre-swapped*:

```cpp
leds[0] = CRGB(g, r, b);   // note: g and r are the wrong way round
```

So `pixel.r` holds your green value and `pixel.g` holds your red value.
FastLED then swaps them again on the way out. Two swaps cancel — but only
if the LED soldered to the board is genuinely RGB-ordered rather than the
GRB the code declares.

In practice that means the answer depends on which WS2812 revision is on
your particular board:

| LED on your board | Sending `r=255` lights | You need |
| --- | --- | --- |
| RGB-ordered | red | `"channel_order": "rgb"` |
| GRB-ordered | green | `"channel_order": "grb"` |

There is no way to detect this from the host side, which is why this
project asks you instead of guessing.

## Calibrating

```bash
buzerkostka calibrate
```

It sends two raw probes and asks what you actually saw:

```
The first probe is on the cube now (raw payload r=255 g=0 b=0).
  Which colour do you see? [r]ed / [g]reen / [b]lue: g

The second probe is on the cube now (raw payload r=0 g=255 b=0).
  Which colour do you see? [r]ed / [g]reen / [b]lue: r

channel order: grb
ok   saved to ~/.config/buzerkostka/config.json
```

The derivation is simple: the channel you *see* for the first probe is the
first letter of the order, the channel you see for the second probe is the
second letter, and the remaining one goes last. All six permutations are
supported, so this also covers BGR and friends if you ever repurpose the
tool for other hardware.

## Checking without calibrating

```bash
buzerkostka test
```

prints the colour it *intends* alongside the raw payload it sent:

```
  red    ██ #FF0000  ->  payload r=0 g=255 b=0
```

If the names match what the cube showed, your `channel_order` is correct.

## Setting it by hand

```json
{ "device": { "channel_order": "grb" } }
```

or, for a one-off:

```bash
BUZERKOSTKA_CHANNEL_ORDER=grb buzerkostka test
```

Remember to `buzerkostka reload` after editing the file so the running
daemon picks it up.
