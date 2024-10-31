
#include <OpenRGB/Client.hpp>
#include <spdlog/spdlog.h>

#include "OpenRGB/Exceptions.hpp"
#include "device-controller.h"

bool DeviceController::ensure_client_connected() {
  if (!client.isConnected()) {
    const auto status = client.connect(config.host, config.port);
    if (status != orgb::ConnectStatus::Success) {
      spdlog::error("Failed to connect to OpenRGB server: {}", (int)status);
      return false;
    }
  }
  return true;
}

const orgb::DeviceList &DeviceController::get_devices() {
  if (!ensure_client_connected()) {
    devices = {};
    return devices;
  }

  try {
    if (!client.isDeviceListOutdatedX())
      return devices;
    devices = client.requestDeviceListX();
  } catch (const orgb::UserError &e) {
    spdlog::error("Failed to request device list: {}", e.errorMessage());
    devices = {};
  } catch (const orgb::ConnectionError &e) {
    spdlog::error("Failed to request device list: {}", e.errorMessage());
    devices = {};
  } catch (const orgb::SystemError &e) {
    spdlog::error("Failed to request device list: {}", e.errorMessage());
    devices = {};
  }

  return devices;
}

std::string
DeviceController::get_device_unique_name(const orgb::Device &device) {
  int num_devices_with_same_name = 0;
  for (const auto &dev : get_devices()) {
    if (dev.name == device.name && dev.idx != device.idx)
      ++num_devices_with_same_name;

    if (dev.idx == device.idx)
      break;
  }

  if (num_devices_with_same_name == 0)
    return device.name;

  return device.name + " (" + std::to_string(num_devices_with_same_name) + ")";
}
