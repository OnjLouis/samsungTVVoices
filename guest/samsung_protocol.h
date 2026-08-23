#pragma once

#include <cstdint>

constexpr std::uint32_t samsungProtocolMagic = 0x56544D53; // SMTV
constexpr std::uint32_t samsungProtocolMaxPayload = 1U << 20;

enum class SamsungMessage : std::uint32_t {
	speak = 1,
	cancel = 2,
	quit = 3,
	ready = 101,
	audio = 102,
	done = 103,
	error = 104,
	cancelled = 105,
};

#pragma pack(push, 1)
struct SamsungMessageHeader {
	std::uint32_t magic;
	std::uint32_t type;
	std::uint32_t generation;
	std::uint32_t size;
};

struct SamsungSpeakOptions {
	std::int32_t language;
	std::int32_t model;
	std::int32_t rate;
	std::int32_t pitch;
	std::int32_t headSize;
};
#pragma pack(pop)

static_assert(sizeof(SamsungMessageHeader) == 16, "Unexpected protocol header size");
static_assert(sizeof(SamsungSpeakOptions) == 20, "Unexpected options size");
