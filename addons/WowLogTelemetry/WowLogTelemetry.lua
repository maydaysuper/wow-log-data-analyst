local addonName = ...
local frame = CreateFrame("Frame")
local elapsed = 0
local SAMPLE_INTERVAL = 1.0
local MAX_SAMPLES = 7200

local function ensureDB()
    if type(WowLogTelemetryDB) ~= "table" then
        WowLogTelemetryDB = {}
    end
    if type(WowLogTelemetryDB.samples) ~= "table" then
        WowLogTelemetryDB.samples = {}
    end
    WowLogTelemetryDB.version = "0.4.0"
end

local function addSample()
    ensureDB()
    local bandwidthIn, bandwidthOut, latencyHome, latencyWorld = GetNetStats()
    local fps = GetFramerate() or 0
    local stamp = date("%Y-%m-%d %H:%M:%S")
    local sample = string.format(
        "%s|%.2f|%.0f|%.0f|%.2f|%.2f",
        stamp,
        fps or 0,
        latencyHome or 0,
        latencyWorld or 0,
        bandwidthIn or 0,
        bandwidthOut or 0
    )
    local samples = WowLogTelemetryDB.samples
    samples[#samples + 1] = sample
    if #samples > MAX_SAMPLES then
        local trim = #samples - MAX_SAMPLES
        for _ = 1, trim do
            table.remove(samples, 1)
        end
    end
end

frame:RegisterEvent("PLAYER_LOGIN")
frame:SetScript("OnEvent", function()
    ensureDB()
end)

frame:SetScript("OnUpdate", function(_, dt)
    elapsed = elapsed + dt
    if elapsed >= SAMPLE_INTERVAL then
        elapsed = elapsed - SAMPLE_INTERVAL
        addSample()
    end
end)

SLASH_WOWLOGTELEMETRY1 = "/wlt"
SlashCmdList.WOWLOGTELEMETRY = function(msg)
    ensureDB()
    msg = (msg or ""):lower()
    if msg == "clear" then
        wipe(WowLogTelemetryDB.samples)
        print("WoW Log Telemetry: samples cleared.")
    else
        print("WoW Log Telemetry: " .. tostring(#WowLogTelemetryDB.samples) .. " samples. Use /wlt clear to reset.")
    end
end
