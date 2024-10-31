#pragma once

#include <OpenRGB/Client.hpp>
#include <OpenRGB/DeviceInfo.hpp>
#include <cstdint>

class DeviceController {
public:
  struct Config {
    const char *host;
    uint16_t port;
  };

  static constexpr Config DEFAULT_CONFIG = {.host = "127.0.0.1", .port = 6742};

  DeviceController(const Config &config = DEFAULT_CONFIG)
      : config(config), client("Rigby Device Controller v0.0.0") {}

  void connect();
  const orgb::DeviceList &get_devices();
  std::string get_device_unique_name(const orgb::Device &device);

protected:
  Config config;
  orgb::Client client;

  orgb::DeviceList devices;
  bool ensure_client_connected();
};
