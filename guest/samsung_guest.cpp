#include "samsung_protocol.h"

#include <atomic>
#include <cerrno>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <unistd.h>

class CSmtSynth {
public:
	CSmtSynth();
	~CSmtSynth();
	int SMTSetLanguage(int language, int mode);
	int SMTModelSelect(int model);
	int SMTSetHybrid(int enabled);
	int SMTInit(char* rootPath, char* libraryPath);
	int SMTSetSamplingRate(int sampleRate);
	int SMTSetChannel(int channels);
	void SMTSetMemory(int size, int count);
	int SMTSetStreamCallback(void (*callback)(short*, int, int));
	int SMTVoiceControl(int pitch, int headSize, int rate);
	void SMTSetControlStart();
	void SMTSetControlStop();
	int SMTStartStreamingPlayUtf8(unsigned char* text, int length, int arg3, int arg4);
	int SMTFree();

private:
	std::uint8_t state_[16];
};

namespace {
struct SpeechJob {
	std::uint32_t generation{};
	SamsungSpeakOptions options{};
	std::string text;
};

int channelFd = -1;
std::mutex outputMutex;
std::mutex jobMutex;
std::condition_variable jobChanged;
SpeechJob pendingJob;
bool hasPendingJob = false;
std::atomic<bool> shuttingDown{false};
std::atomic<bool> cancelling{false};
std::atomic<std::uint32_t> activeGeneration{0};

bool writeExact(const void* data, std::size_t size) {
	const auto* bytes = static_cast<const std::uint8_t*>(data);
	while (size > 0) {
		const ssize_t written = ::write(channelFd, bytes, size);
		if (written < 0 && errno == EINTR) {
			continue;
		}
		if (written <= 0) {
			return false;
		}
		bytes += written;
		size -= static_cast<std::size_t>(written);
	}
	return true;
}

bool readExact(void* data, std::size_t size) {
	auto* bytes = static_cast<std::uint8_t*>(data);
	while (size > 0) {
		const ssize_t received = ::read(channelFd, bytes, size);
		if (received < 0 && errno == EINTR) {
			continue;
		}
		if (received <= 0) {
			return false;
		}
		bytes += received;
		size -= static_cast<std::size_t>(received);
	}
	return true;
}

bool sendMessage(
	SamsungMessage type,
	std::uint32_t generation,
	const void* payload = nullptr,
	std::uint32_t size = 0
) {
	const SamsungMessageHeader header{
		samsungProtocolMagic,
		static_cast<std::uint32_t>(type),
		generation,
		size,
	};
	std::lock_guard<std::mutex> lock(outputMutex);
	return writeExact(&header, sizeof(header))
		&& (size == 0 || writeExact(payload, size));
}

void sendError(std::uint32_t generation, const char* message) {
	sendMessage(
		SamsungMessage::error,
		generation,
		message,
		static_cast<std::uint32_t>(std::strlen(message))
	);
}

void streamCallback(short* samples, int count, int) {
	const std::uint32_t generation = activeGeneration.load(std::memory_order_acquire);
	if (generation == 0 || cancelling.load(std::memory_order_relaxed) || count <= 0 || samples == nullptr) {
		return;
	}
	const std::uint32_t bytes = static_cast<std::uint32_t>(count) * sizeof(short);
	sendMessage(SamsungMessage::audio, generation, samples, bytes);
}

bool initializeSynth(CSmtSynth& synth, const SamsungSpeakOptions& options) {
	if (synth.SMTSetLanguage(options.language, 0) != 1
		|| synth.SMTSetHybrid(0) != 1
		|| synth.SMTModelSelect(options.model) != 1) {
		return false;
	}
	char root[] = "/opt/smt/runtime/";
	char libs[] = "/opt/smt/runtime/Lib/";
	if (synth.SMTInit(root, libs) != 1
		|| synth.SMTSetSamplingRate(48000) != 1
		|| synth.SMTSetChannel(1) != 1) {
		return false;
	}
	synth.SMTSetMemory(12000, 8);
	if (synth.SMTSetStreamCallback(streamCallback) != 1) {
		return false;
	}
	return true;
}

void releaseSynth(std::unique_ptr<CSmtSynth>& synth) {
	if (!synth) {
		return;
	}
	synth->SMTFree();
	synth.reset();
}

std::size_t nextChunkEnd(const std::string& text, std::size_t start) {
	constexpr std::size_t targetBytes = 160;
	if (text.size() - start <= targetBytes) {
		return text.size();
	}
	std::size_t end = start + targetBytes;
	while (end > start && (static_cast<unsigned char>(text[end]) & 0xC0) == 0x80) {
		--end;
	}
	const std::size_t minimumBreak = start + targetBytes / 2;
	for (std::size_t position = end; position > minimumBreak; --position) {
		const char character = text[position - 1];
		if (character == ' ' || character == '\n' || character == '\t') {
			return position;
		}
	}
	return end;
}

void synthesisWorker() {
	std::unique_ptr<CSmtSynth> synth;
	int currentLanguage = -1;
	int currentModel = -1;
	while (!shuttingDown.load(std::memory_order_acquire)) {
		SpeechJob job;
		{
			std::unique_lock<std::mutex> lock(jobMutex);
			jobChanged.wait(lock, [] { return hasPendingJob || shuttingDown.load(); });
			if (shuttingDown.load()) {
				break;
			}
			job = std::move(pendingJob);
			hasPendingJob = false;
		}

		cancelling.store(false, std::memory_order_release);
		activeGeneration.store(job.generation, std::memory_order_release);
		if (!synth || currentLanguage != job.options.language || currentModel != job.options.model) {
			releaseSynth(synth);
			synth = std::make_unique<CSmtSynth>();
			if (initializeSynth(*synth, job.options)) {
				currentLanguage = job.options.language;
				currentModel = job.options.model;
			} else {
				synth.reset();
				currentLanguage = -1;
				currentModel = -1;
			}
		}

		if (!synth) {
			sendError(job.generation, "The Samsung voice could not be initialized.");
		} else {
			// The television service uses the first control for pitch and the third
			// for rate. The undocumented second control shifts vocal-tract size.
			// Volume is applied by NVDA because the engine has no reliable gain control.
			synth->SMTVoiceControl(job.options.pitch, job.options.headSize, job.options.rate);
			synth->SMTSetControlStart();
			bool succeeded = true;
			std::size_t offset = 0;
			while (offset < job.text.size() && !cancelling.load(std::memory_order_acquire)) {
				const std::size_t end = nextChunkEnd(job.text, offset);
				std::string chunk = job.text.substr(offset, end - offset);
				const int result = synth->SMTStartStreamingPlayUtf8(
					reinterpret_cast<unsigned char*>(&chunk[0]),
					static_cast<int>(chunk.size()),
					0,
					0
				);
				if (result != 1) {
					succeeded = false;
					break;
				}
				offset = end;
			}
			synth->SMTSetControlStop();
			if (!cancelling.load(std::memory_order_acquire)) {
				if (succeeded) {
					sendMessage(SamsungMessage::done, job.generation);
				} else {
					sendError(job.generation, "Samsung synthesis failed.");
				}
			}
		}
		activeGeneration.store(0, std::memory_order_release);
	}
	releaseSynth(synth);
}

void cancelActive(std::uint32_t generation) {
	if (activeGeneration.load(std::memory_order_acquire) != generation) {
		return;
	}
	cancelling.store(true, std::memory_order_release);
	sendMessage(SamsungMessage::cancelled, generation);
}
} // namespace

int main() {
	channelFd = ::open("/dev/vport0p1", O_RDWR);
	if (channelFd < 0 || !sendMessage(SamsungMessage::ready, 0)) {
		return 2;
	}

	std::thread worker(synthesisWorker);
	while (!shuttingDown.load(std::memory_order_acquire)) {
		SamsungMessageHeader header{};
		if (!readExact(&header, sizeof(header))) {
			break;
		}
		if (header.magic != samsungProtocolMagic || header.size > samsungProtocolMaxPayload) {
			break;
		}
		std::vector<std::uint8_t> payload(header.size);
		if (header.size != 0 && !readExact(payload.data(), payload.size())) {
			break;
		}

		const auto type = static_cast<SamsungMessage>(header.type);
		if (type == SamsungMessage::speak) {
			if (payload.size() <= sizeof(SamsungSpeakOptions)) {
				sendError(header.generation, "The speech request was empty.");
				continue;
			}
			SpeechJob job;
			job.generation = header.generation;
			std::memcpy(&job.options, payload.data(), sizeof(job.options));
			job.text.assign(
				reinterpret_cast<const char*>(payload.data() + sizeof(job.options)),
				payload.size() - sizeof(job.options)
			);
			{
				std::lock_guard<std::mutex> lock(jobMutex);
				pendingJob = std::move(job);
				hasPendingJob = true;
			}
			jobChanged.notify_one();
		} else if (type == SamsungMessage::cancel) {
			cancelActive(header.generation);
		} else if (type == SamsungMessage::quit) {
			shuttingDown.store(true, std::memory_order_release);
			cancelActive(activeGeneration.load(std::memory_order_acquire));
			jobChanged.notify_all();
			break;
		}
	}

	shuttingDown.store(true, std::memory_order_release);
	cancelActive(activeGeneration.load(std::memory_order_acquire));
	jobChanged.notify_all();
	worker.join();
	::close(channelFd);
	return 0;
}
