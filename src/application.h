#pragma once

#include "device-controller.h"
#include <SDL.h>

class Application {
public:
  struct Config {
    DeviceController::Config device_controller;
  };

  constexpr static Config DEFAULT_CONFIG = {
      .device_controller = DeviceController::DEFAULT_CONFIG};

  static Application &get_instance() {
    static auto instance = Application();
    return instance;
  }

  void initialize(SDL_Renderer *renderer);
  void render();

private:
  Application(const Config &config = DEFAULT_CONFIG)
      : device_controller(config.device_controller){};

  struct ViewState {
    enum class SelectedTab {
      Devices,
      Effects,
      Settings,
    } selected_tab = SelectedTab::Devices;
    struct {
      size_t selected_device = 0;
      size_t highlighted_device = 0;
    } devices;
  };

protected:
  SDL_Renderer *renderer = nullptr;
  DeviceController device_controller;

  void render_device_tab();
  void render_device_tab__device_select(const orgb::DeviceList &devices);
  void render_device_tab__device_info(const orgb::DeviceList &devices);

  ViewState state;
};
