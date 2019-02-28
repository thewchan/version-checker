Feature: Update defined files with new versions of components
  In order to automate components versions upgrading
  As a maintainer
  I want to versions number to be updated accordingly to configuration file

  Scenario: Update version in defined files for component
     Given New version of component is set in config file
      When script is run in update mode
      Then replace version in files defined in config files