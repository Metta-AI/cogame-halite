## cogame-halite static replay renderer (Nim -> wasm).
##
## Renders one recorded turn as a Bitworld sprite-protocol v1 packet
## (bitworld/spriteprotocol) that coworld-ctf's client/broadcast_core.js draws
## unchanged: a zoomable map layer holding the baked seabed bands (object ids
## 40.., z = -32768 — the client's static-band cache) plus one object per
## halite cell, hull, dock, cargo pip and at-risk halo, all drawn from the
## nano-banana sheets in data/art (preloaded by --preload-file).
##
## **Nothing is re-simulated here.** The wasm renderer draws the *recorded*
## per-turn state; the page owns playback (which turn) and says so on the text
## command channel (0x81): `s:<turn>` and `r:<0|1>` (cargo-at-risk overlay).
## Chrome JSON rides the reserved sprite 4090's label, as in ctf.
##
## Export surface (the same shape as ctf_replay.nim, renamed): halite_load_replay,
## halite_frame, halite_input, halite_packet_ptr/len, halite_error_ptr/len,
## halite_stage_ptr/len.

import
  std/[json, math, sets, strutils, tables],
  bitworld/spriteprotocol, pixie

const
  ChromeSpriteId = 4090        ## label carries chrome JSON (ctf convention)
  MapLayerId = 0
  MapLayerType = 0
  ZoomableFlag = 1
  BandObjectBase = 40
  MaxBands = 24
  StaticBandZ = -32768

  CellPx = 32                  ## board pixels per cell (21 x 32 = 672)
  SpriteSrcPx = 64             ## the art sheets are 64 px per kit

  CrystalSpriteBase = 100      ## + density step 1..5
  HullSpriteBase = 110         ## + seat
  YardSpriteBase = 120         ## + seat
  PipSpriteBase = 130          ## + cargo bucket 0..5
  HaloSpriteId = 140
  WashSpriteId = 141

  CrystalObjectBase = 1000     ## + cell index
  WashObjectBase = 2000        ## + cell index
  YardObjectBase = 3000
  HullObjectBase = 4000
  PipObjectBase = 5000
  HaloObjectBase = 6000

  MaxSeats = 4
  MaxCells = 4096              ## 21x21 = 441; the guard is for a bad replay
  ## Cell halite -> density step (design note §What it draws).
  DensityCuts = [0, 1, 50, 150, 300, 450]
  ## Cargo -> pip bucket. 0 means "no pip".
  CargoCuts = [0, 1, 100, 300, 700, 1500]

type
  Rgba = object
    ## Straight-alpha RGBA pixel buffer (what the wire wants).
    w, h: int
    data: seq[uint8]

  ShipRec = object
    id: string
    seat: int
    pos: int
    cargo: int

  YardRec = object
    id: string
    seat: int
    pos: int

  TurnRec = object
    t: int
    halite: seq[int]
    banks: array[MaxSeats, int]
    ships: seq[ShipRec]
    yards: seq[YardRec]

  Replay = object
    size: int
    seats: int
    turns: seq[TurnRec]

var
  runtimeLoaded = false
  replay: Replay
  packet: seq[uint8]
  lastError: string
  boardW, boardH: int
  bandsEmitted = false
  spritesEmitted = false
  curTurn = 0
  riskOverlay = true
  dirty = true
  liveObjects: HashSet[int]
  seabed: Rgba
  crystalKits: array[6, Rgba]
  hullKits: array[MaxSeats, Rgba]
  yardKits: array[MaxSeats, Rgba]

## --- Progress stage note (see ctf_replay.nim: survives an ABORTING_MALLOC
## abort, so JS can report what the runtime was doing) ---
var
  stageNote: array[192, char]
  stageNoteLen: int
  currentStage: string

proc stampStage(stage: string) =
  currentStage = stage
  stageNoteLen = min(stage.len, stageNote.len)
  if stageNoteLen > 0:
    copyMem(stageNote[0].addr, stage[0].unsafeAddr, stageNoteLen)

proc bytesFromPointer(data: ptr uint8, length: int): string =
  result = newString(length)
  if length > 0:
    copyMem(result[0].addr, data, length)

# ---------------------------------------------------------------------------
# Pixel buffers

proc newRgba(w, h: int): Rgba =
  Rgba(w: w, h: h, data: newSeq[uint8](w * h * 4))

proc blit(dst: var Rgba, src: Rgba, dx, dy: int) =
  ## Source-over blit of the whole of src at (dx, dy).
  for y in 0 ..< src.h:
    let ty = dy + y
    if ty < 0 or ty >= dst.h: continue
    for x in 0 ..< src.w:
      let tx = dx + x
      if tx < 0 or tx >= dst.w: continue
      let si = (y * src.w + x) * 4
      let sa = int(src.data[si + 3])
      if sa == 0: continue
      let di = (ty * dst.w + tx) * 4
      if sa == 255:
        dst.data[di] = src.data[si]
        dst.data[di + 1] = src.data[si + 1]
        dst.data[di + 2] = src.data[si + 2]
        dst.data[di + 3] = 255
      else:
        let da = int(dst.data[di + 3])
        let outA = sa + da * (255 - sa) div 255
        if outA == 0: continue
        for c in 0 .. 2:
          let sc = int(src.data[si + c])
          let dc = int(dst.data[di + c])
          dst.data[di + c] = uint8((sc * sa + dc * da * (255 - sa) div 255) div outA)
        dst.data[di + 3] = uint8(outA)

proc toStraightRgba(image: Image): Rgba =
  ## pixie images are premultiplied RGBX; the wire is straight RGBA.
  result = newRgba(image.width, image.height)
  for i in 0 ..< image.width * image.height:
    let px = image.data[i]
    let a = int(px.a)
    let o = i * 4
    if a == 0: continue
    result.data[o] = uint8(min(255, int(px.r) * 255 div a))
    result.data[o + 1] = uint8(min(255, int(px.g) * 255 div a))
    result.data[o + 2] = uint8(min(255, int(px.b) * 255 div a))
    result.data[o + 3] = uint8(a)

proc loadScaled(path: string, size: int): Rgba =
  let image = decodeImage(readFile(path))
  if image.width == size and image.height == size:
    return image.toStraightRgba()
  image.resize(size, size).toStraightRgba()

proc loadFrames(path: string, frames, size: int): seq[Rgba] =
  ## Splits a horizontal strip of `frames` square frames and scales each.
  let sheet = decodeImage(readFile(path))
  let src = sheet.width div frames
  for i in 0 ..< frames:
    var frame = newImage(src, src)
    frame.draw(sheet, translate(vec2(-float32(i * src), 0)))
    if src == size:
      result.add frame.toStraightRgba()
    else:
      result.add frame.resize(size, size).toStraightRgba()

proc solidDisc(size: int, r, g, b, a: int): Rgba =
  ## A soft radial disc — the at-risk halo and the ram-lane wash.
  result = newRgba(size, size)
  let c = float(size - 1) / 2.0
  for y in 0 ..< size:
    for x in 0 ..< size:
      let dx = float(x) - c
      let dy = float(y) - c
      let d = sqrt(dx * dx + dy * dy) / c
      if d > 1.0: continue
      let fade = 1.0 - d * d
      let o = (y * size + x) * 4
      result.data[o] = uint8(r)
      result.data[o + 1] = uint8(g)
      result.data[o + 2] = uint8(b)
      result.data[o + 3] = uint8(float(a) * fade)

proc solidRect(w, h, r, g, b, a: int): Rgba =
  result = newRgba(w, h)
  for i in 0 ..< w * h:
    result.data[i * 4] = uint8(r)
    result.data[i * 4 + 1] = uint8(g)
    result.data[i * 4 + 2] = uint8(b)
    result.data[i * 4 + 3] = uint8(a)

# ---------------------------------------------------------------------------
# Art

proc loadArt() =
  stampStage("load board art")
  seabed = loadScaled("data/art/seabed.png", 256)
  let frames = loadFrames("data/art/halite_crystals.png", 6, CellPx)
  for i in 0 ..< 6:
    crystalKits[i] = frames[i]
  const SeatNames = ["alpha", "bravo", "charlie", "delta"]
  for seat in 0 ..< MaxSeats:
    hullKits[seat] = loadScaled("data/art/hull_" & SeatNames[seat] & ".png", CellPx)
    yardKits[seat] = loadScaled("data/art/yard_" & SeatNames[seat] & ".png", CellPx)

# ---------------------------------------------------------------------------
# Replay parsing

proc parseReplay(text: string): Replay =
  let doc = parseJson(text)
  if doc{"format"}.getStr != "cogame-halite-replay":
    raise newException(ValueError,
      "not a cogame-halite replay (format=" & doc{"format"}.getStr & ")")
  let config = doc{"config"}
  result.size = if config != nil: config{"size"}.getInt(21) else: 21
  if result.size <= 0 or result.size * result.size > MaxCells:
    raise newException(ValueError, "replay board size out of range: " & $result.size)
  let turns = doc{"turns"}
  if turns == nil or turns.kind != JArray or turns.len == 0:
    raise newException(ValueError, "replay has no turns")
  for node in turns:
    var rec = TurnRec(t: node{"t"}.getInt)
    for cell in node{"halite"}:
      rec.halite.add cell.getInt
    var seat = 0
    for player in node{"players"}:
      if player.kind != JArray or player.len < 3: continue
      if seat < MaxSeats:
        rec.banks[seat] = player[0].getInt
      for yid, pos in player[1]:
        rec.yards.add YardRec(id: yid, seat: seat, pos: pos.getInt)
      for sid, entry in player[2]:
        if entry.kind != JArray or entry.len < 2: continue
        rec.ships.add ShipRec(id: sid, seat: seat,
                              pos: entry[0].getInt, cargo: entry[1].getInt)
      inc seat
    result.seats = max(result.seats, seat)
    result.turns.add rec

# ---------------------------------------------------------------------------
# Geometry. Board index = (size - y - 1) * size + x, so index 0 is the TOP-LEFT
# cell on screen and the halite array draws in raster order (design note
# §Geometry, exactly).

proc cellX(pos: int): int = (pos mod replay.size) * CellPx
proc cellY(pos: int): int = (pos div replay.size) * CellPx
proc cellRow(pos: int): int = pos div replay.size

proc torusStep(pos, dx, dy: int): int =
  let size = replay.size
  let col = (pos mod size + dx + size) mod size
  let row = (pos div size + dy + size) mod size
  row * size + col

proc densityStep(halite: int): int =
  result = 0
  for i in countdown(DensityCuts.len - 1, 1):
    if halite >= DensityCuts[i]:
      return i

proc cargoBucket(cargo: int): int =
  result = 0
  for i in countdown(CargoCuts.len - 1, 1):
    if cargo >= CargoCuts[i]:
      return i

# ---------------------------------------------------------------------------
# Static layers

proc bakeSeabed(): Rgba =
  stampStage("bake seabed (" & $boardW & "x" & $boardH & ")")
  result = newRgba(boardW, boardH)
  var y = 0
  while y < boardH:
    var x = 0
    while x < boardW:
      result.blit(seabed, x, y)
      x += seabed.w
    y += seabed.h

proc emitBands() =
  let floor = bakeSeabed()
  stampStage("emit seabed bands")
  var bandH = max(CellPx, (boardH + MaxBands - 1) div MaxBands)
  bandH = ((bandH + CellPx - 1) div CellPx) * CellPx
  var y = 0
  var band = 0
  while y < boardH and band < MaxBands:
    let h = min(bandH, boardH - y)
    packet.addSprite(BandObjectBase + band, boardW, h,
      floor.data.toOpenArray(y * boardW * 4, (y + h) * boardW * 4 - 1),
      "seabed band " & $band)
    packet.addObject(BandObjectBase + band, 0, y, StaticBandZ, MapLayerId,
                     BandObjectBase + band)
    y += h
    inc band
  bandsEmitted = true

proc emitSprites() =
  stampStage("define sprites")
  for step in 1 .. 5:
    packet.addSprite(CrystalSpriteBase + step, crystalKits[step].w,
      crystalKits[step].h, crystalKits[step].data, "halite " & $step)
  for seat in 0 ..< MaxSeats:
    packet.addSprite(HullSpriteBase + seat, hullKits[seat].w, hullKits[seat].h,
      hullKits[seat].data, "hull " & $seat)
    packet.addSprite(YardSpriteBase + seat, yardKits[seat].w, yardKits[seat].h,
      yardKits[seat].data, "yard " & $seat)
  # Cargo pips: the hold's weight, drawn as a growing bright disc on the deck.
  for bucket in 1 .. 5:
    let size = 6 + bucket * 3
    let pip = solidDisc(size, 255, 246, 214, 150 + bucket * 20)
    packet.addSprite(PipSpriteBase + bucket, pip.w, pip.h, pip.data, "pip " & $bucket)
  let halo = solidDisc(CellPx + 16, 232, 70, 60, 190)
  packet.addSprite(HaloSpriteId, halo.w, halo.h, halo.data, "at-risk halo")
  let wash = solidRect(CellPx, CellPx, 216, 60, 52, 60)
  packet.addSprite(WashSpriteId, wash.w, wash.h, wash.data, "ram lane")
  spritesEmitted = true

# ---------------------------------------------------------------------------
# The recorded turn

proc place(seen: var HashSet[int], id, x, y, z, spriteId: int) =
  seen.incl id
  liveObjects.incl id
  packet.addObject(id, clamp(x, -32000, 32000), clamp(y, -32000, 32000),
                   clamp(z, -32000, 32000), MapLayerId, spriteId)

proc emitTurn() =
  stampStage("draw turn " & $curTurn)
  let rec = replay.turns[clamp(curTurn, 0, replay.turns.len - 1)]
  var seen: HashSet[int]

  for index, halite in rec.halite:
    let step = densityStep(halite)
    if step <= 0: continue
    seen.place(CrystalObjectBase + index, cellX(index), cellY(index), 0,
               CrystalSpriteBase + step)

  # Cargo-at-risk (the idea's ask, in v1), derived from the RECORDED state: a
  # ship is at risk iff some enemy ship with cargo <= its own is within torus
  # Manhattan distance 1 — the exact predicate the ram rule uses next turn.
  var atRisk = newSeq[bool](rec.ships.len)
  if riskOverlay:
    var byCell = initTable[int, seq[int]]()
    for i, ship in rec.ships:
      byCell.mgetOrPut(ship.pos, @[]).add i
    for i, ship in rec.ships:
      if ship.cargo <= 0: continue
      block scan:
        for (dx, dy) in [(0, 0), (0, -1), (0, 1), (-1, 0), (1, 0)]:
          let cell = torusStep(ship.pos, dx, dy)
          if cell notin byCell: continue
          for j in byCell[cell]:
            let other = rec.ships[j]
            if other.seat == ship.seat: continue
            if other.cargo <= ship.cargo:
              atRisk[i] = true
              break scan

  for i, ship in rec.ships:
    if not atRisk[i]: continue
    # The <= 4 cells a lighter enemy could ram them from get a faint red wash.
    for (dx, dy) in [(0, -1), (0, 1), (-1, 0), (1, 0)]:
      let cell = torusStep(ship.pos, dx, dy)
      seen.place(WashObjectBase + cell, cellX(cell), cellY(cell), 1, WashSpriteId)

  for k, yard in rec.yards:
    seen.place(YardObjectBase + k, cellX(yard.pos), cellY(yard.pos), 2,
               YardSpriteBase + (yard.seat mod MaxSeats))

  for k, ship in rec.ships:
    let z = 10 + cellRow(ship.pos) * 4
    if atRisk[k]:
      seen.place(HaloObjectBase + k, cellX(ship.pos) - 8, cellY(ship.pos) - 8,
                 z, HaloSpriteId)
    seen.place(HullObjectBase + k, cellX(ship.pos), cellY(ship.pos), z + 1,
               HullSpriteBase + (ship.seat mod MaxSeats))
    let bucket = cargoBucket(ship.cargo)
    if bucket > 0:
      let size = 6 + bucket * 3
      seen.place(PipObjectBase + k, cellX(ship.pos) + (CellPx - size) div 2,
                 cellY(ship.pos) + (CellPx - size) div 2, z + 2,
                 PipSpriteBase + bucket)

  var gone: seq[int]
  for id in liveObjects:
    if id notin seen: gone.add id
  for id in gone:
    packet.addDeleteObject(id)
    liveObjects.excl id

proc chromeJson(): string =
  let rec = replay.turns[clamp(curTurn, 0, replay.turns.len - 1)]
  var banks = newJArray()
  for seat in 0 ..< max(1, replay.seats):
    banks.add newJInt(rec.banks[seat])
  var risk = newJArray()
  for seat in 0 ..< max(1, replay.seats):
    var total = 0
    for ship in rec.ships:
      if ship.seat == seat: total += ship.cargo
    risk.add newJInt(total)
  let node = %*{
    "kind": "halite",
    "t": rec.t,
    "n": replay.turns.len,
    "size": replay.size,
    "cell": CellPx,
    "risk": riskOverlay,
    "banks": banks,
    "afloat": risk,
    "board": {"w": boardW, "h": boardH},
  }
  $node

proc renderCurrent() =
  packet.setLen(0)
  if not bandsEmitted:
    packet.addLayer(MapLayerId, MapLayerType, ZoomableFlag)
    packet.addViewport(MapLayerId, boardW, boardH)
    emitBands()
  if not spritesEmitted:
    emitSprites()
  if dirty:
    emitTurn()
    dirty = false
  packet.addSprite(ChromeSpriteId, 1, 1, [0'u8, 0, 0, 0], chromeJson())

# ---------------------------------------------------------------------------
# Exports

proc haliteLoadReplay(data: ptr uint8, length: cint): cint
    {.exportc: "halite_load_replay", cdecl.} =
  try:
    lastError = ""
    runtimeLoaded = false
    currentStage = ""
    stampStage("load art")
    loadArt()
    stampStage("parse replay")
    replay = parseReplay(data.bytesFromPointer(int(length)))
    boardW = replay.size * CellPx
    boardH = replay.size * CellPx
    bandsEmitted = false
    spritesEmitted = false
    liveObjects.clear()
    curTurn = 0
    dirty = true
    stampStage("render first frame (" & $boardW & "x" & $boardH & ")")
    renderCurrent()
    stampStage("loaded")
    runtimeLoaded = true
    return 1
  except Exception as error:
    runtimeLoaded = false
    lastError = currentStage & ": " & error.msg & "\n" & error.getStackTrace()
    return 0

proc applyCommand(text: string) =
  if text.startsWith("s:"):
    let value = try: parseInt(text[2 .. ^1]) except ValueError: -1
    if value >= 0 and value != curTurn:
      curTurn = clamp(value, 0, replay.turns.len - 1)
      dirty = true
  elif text.startsWith("r:"):
    let want = text.len > 2 and text[2] != '0'
    if want != riskOverlay:
      riskOverlay = want
      dirty = true

proc haliteInput(data: ptr uint8, length: cint)
    {.exportc: "halite_input", cdecl.} =
  if not runtimeLoaded: return
  try:
    for item in data.bytesFromPointer(int(length)).parseSpriteClientMessages():
      if item.kind == SpriteClientChatMessage:
        applyCommand(item.text)
  except Exception:
    discard

proc haliteFrame(): cint {.exportc: "halite_frame", cdecl.} =
  if not runtimeLoaded:
    return 0
  try:
    renderCurrent()
    return 1
  except Exception as error:
    lastError = "render turn: " & error.msg & "\n" & error.getStackTrace()
    return -1

proc halitePacketPointer(): ptr uint8 {.exportc: "halite_packet_ptr", cdecl.} =
  if packet.len == 0: nil else: packet[0].addr

proc halitePacketLength(): cint {.exportc: "halite_packet_len", cdecl.} =
  cint(packet.len)

proc haliteErrorPointer(): ptr uint8 {.exportc: "halite_error_ptr", cdecl.} =
  if lastError.len == 0: nil else: cast[ptr uint8](lastError[0].addr)

proc haliteErrorLength(): cint {.exportc: "halite_error_len", cdecl.} =
  cint(lastError.len)

proc haliteStagePointer(): ptr uint8 {.exportc: "halite_stage_ptr", cdecl.} =
  ## Unlike halite_error_*, this stays valid after an allocation-failure abort,
  ## so JS can report what the runtime was doing when the address space ran out.
  if stageNoteLen == 0: nil else: cast[ptr uint8](stageNote[0].addr)

proc haliteStageLength(): cint {.exportc: "halite_stage_len", cdecl.} =
  cint(stageNoteLen)

when defined(emscripten):
  proc emscriptenExitWithLiveRuntime() {.
    importc: "emscripten_exit_with_live_runtime", cdecl.}

when isMainModule and defined(emscripten):
  # See ctf_replay.nim: Nim's generated main runs every module-global
  # destructor when it returns, freeing the art and the replay while the wasm
  # module stays alive and JS keeps calling in. Exit with the runtime alive so
  # the globals live for the page's lifetime.
  emscriptenExitWithLiveRuntime()

when isMainModule and not defined(emscripten):
  # Native smoke: `nim c -r replay-viewer/halite_replay.nim <replay.json>`
  # loads a replay and renders a few turns (64-bit sanity; the wasm32 truth is
  # the wasm-viewer CI job).
  import std/os
  if paramCount() >= 1:
    setCurrentDir(currentSourcePath().parentDir().parentDir())
    let bytes = readFile(paramStr(1))
    if haliteLoadReplay(cast[ptr uint8](bytes[0].unsafeAddr), cint(bytes.len)) != 1:
      echo "load failed: ", lastError
      quit(1)
    echo "first packet ", packet.len, " bytes; board ", boardW, "x", boardH,
         "; turns ", replay.turns.len
    for i in 1 .. 3:
      let cmd = "s:" & $i
      var msg: seq[uint8]
      msg.add 0x81'u8
      msg.addU16(cmd.len)
      for ch in cmd: msg.add uint8(ord(ch))
      haliteInput(msg[0].addr, cint(msg.len))
      doAssert haliteFrame() == 1, lastError
      echo "turn ", i, " packet ", packet.len, " bytes"
