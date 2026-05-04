-- Brooklyn Bikery Database Schema
-- MySQL database structure for customer management and service tracking

-- Create database
CREATE DATABASE IF NOT EXISTS `bikeshop` 
DEFAULT CHARACTER SET utf8mb4 
COLLATE utf8mb4_0900_ai_ci;

USE `bikeshop`;

-- Customers table
-- Stores customer contact information with unique phone constraint
CREATE TABLE `customers` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) DEFAULT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `date_created` date DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `phone` (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Orders table
-- Tracks service requests with front/rear component separation
-- Boolean fields (tinyint) indicate which services were performed
CREATE TABLE `orders` (
  `id` int NOT NULL AUTO_INCREMENT,
  `customer_id` int DEFAULT NULL,
  `date_of_service` date DEFAULT NULL,
  `bike_description` varchar(255) DEFAULT NULL,
  
  -- Flat tire repairs
  `front_flat` tinyint(1) DEFAULT '0',
  `rear_flat` tinyint(1) DEFAULT '0',
  `front_flat_ebike` tinyint(1) DEFAULT '0',
  `rear_flat_ebike` tinyint(1) DEFAULT '0',
  
  -- Tune-up
  `tune_up` tinyint(1) DEFAULT '0',
  
  -- Brake adjustments
  `front_brake_adj` tinyint(1) DEFAULT '0',
  `rear_brake_adj` tinyint(1) DEFAULT '0',
  `front_brake_adj_ebike` tinyint(1) DEFAULT '0',
  `rear_brake_adj_ebike` tinyint(1) DEFAULT '0',
  
  -- Brake pad services
  `front_replace_vbrake_pads` tinyint(1) DEFAULT '0',
  `rear_replace_vbrake_pads` tinyint(1) DEFAULT '0',
  `front_new_vbrake_pads` tinyint(1) DEFAULT '0',
  `rear_new_vbrake_pads` tinyint(1) DEFAULT '0',
  `front_replace_disc_pads` tinyint(1) DEFAULT '0',
  `rear_replace_disc_pads` tinyint(1) DEFAULT '0',
  `front_new_disc_pads` tinyint(1) DEFAULT '0',
  `rear_new_disc_pads` tinyint(1) DEFAULT '0',
  `front_hydraulic_brake_bleed` tinyint(1) DEFAULT '0',
  `rear_hydraulic_brake_bleed` tinyint(1) DEFAULT '0',
  
  -- Derailleur and shifting
  `front_derailleur_adj` tinyint(1) DEFAULT '0',
  `rear_derailleur_adj` tinyint(1) DEFAULT '0',
  
  -- Wheel services
  `front_wheel_true` tinyint(1) DEFAULT '0',
  `rear_wheel_true` tinyint(1) DEFAULT '0',
  `front_repack_wheel` tinyint(1) DEFAULT '0',
  `rear_repack_wheel` tinyint(1) DEFAULT '0',
  
  -- Drivetrain
  `replace_crank_bb` tinyint(1) DEFAULT '0',
  `new_bb` tinyint(1) DEFAULT '0',
  `replace_chain` tinyint(1) DEFAULT '0',
  `replace_cassette` tinyint(1) DEFAULT '0',
  
  -- Cables and lines
  `replace_front_brake_line` tinyint(1) DEFAULT '0',
  `replace_rear_brake_line` tinyint(1) DEFAULT '0',
  `replace_front_gear_line` tinyint(1) DEFAULT '0',
  `replace_rear_gear_line` tinyint(1) DEFAULT '0',
  
  -- Headset
  `repack_headset` tinyint(1) DEFAULT '0',
  `repack_headset_ebike` tinyint(1) DEFAULT '0',
  
  -- Disc brake rotors
  `replace_front_rotor` tinyint(1) DEFAULT '0',
  `replace_rear_rotor` tinyint(1) DEFAULT '0',
  
  -- E-bike and assembly
  `ebike_diagnostic` tinyint(1) DEFAULT '0',
  `bike_assembly` tinyint(1) DEFAULT '0',
  `ebike_assembly` tinyint(1) DEFAULT '0',
  
  -- Spoke repairs (integer count, pricing: $35 base + $2 per spoke)
  `front_fix_spoke` int DEFAULT '0',
  `rear_fix_spoke` int DEFAULT '0',
  
  -- Custom services
  `custom_service` tinyint(1) DEFAULT '0',
  `custom_description` varchar(255) DEFAULT NULL,
  
  -- Notes
  `customer_notes` text,
  `backend_notes` text,
  
  PRIMARY KEY (`id`),
  KEY `customer_id` (`customer_id`),
  CONSTRAINT `orders_ibfk_1` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Payments table
-- Stores pricing for each service performed
-- ID matches order ID (1-to-1 relationship)
CREATE TABLE `payments` (
  `id` int NOT NULL AUTO_INCREMENT,
  
  -- Flat tire repair prices
  `front_flat_price` decimal(6,2) DEFAULT NULL,
  `rear_flat_price` decimal(6,2) DEFAULT NULL,
  `front_flat_ebike_price` decimal(6,2) DEFAULT NULL,
  `rear_flat_ebike_price` decimal(6,2) DEFAULT NULL,
  
  -- Tune-up price
  `tune_up_price` decimal(6,2) DEFAULT NULL,
  
  -- Brake adjustment prices
  `front_brake_adj_price` decimal(6,2) DEFAULT NULL,
  `rear_brake_adj_price` decimal(6,2) DEFAULT NULL,
  `front_brake_adj_ebike_price` decimal(6,2) DEFAULT NULL,
  `rear_brake_adj_ebike_price` decimal(6,2) DEFAULT NULL,
  
  -- Brake pad prices
  `front_replace_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_replace_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `front_new_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_new_vbrake_pads_price` decimal(6,2) DEFAULT NULL,
  `front_replace_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_replace_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `front_new_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `rear_new_disc_pads_price` decimal(6,2) DEFAULT NULL,
  `front_hydraulic_brake_bleed_price` decimal(6,2) DEFAULT NULL,
  `rear_hydraulic_brake_bleed_price` decimal(6,2) DEFAULT NULL,
  
  -- Derailleur prices
  `front_derailleur_adj_price` decimal(6,2) DEFAULT NULL,
  `rear_derailleur_adj_price` decimal(6,2) DEFAULT NULL,
  
  -- Wheel service prices
  `front_wheel_true_price` decimal(6,2) DEFAULT NULL,
  `rear_wheel_true_price` decimal(6,2) DEFAULT NULL,
  `front_repack_wheel_price` decimal(10,2) DEFAULT NULL,
  `rear_repack_wheel_price` decimal(10,2) DEFAULT NULL,
  
  -- Drivetrain prices
  `replace_crank_bb_price` decimal(6,2) DEFAULT NULL,
  `new_bb_price` decimal(6,2) DEFAULT NULL,
  `replace_chain_price` decimal(6,2) DEFAULT NULL,
  `replace_cassette_price` decimal(6,2) DEFAULT NULL,
  
  -- Cable and line prices
  `replace_front_brake_line_price` decimal(6,2) DEFAULT NULL,
  `replace_rear_brake_line_price` decimal(6,2) DEFAULT NULL,
  `replace_front_gear_line_price` decimal(6,2) DEFAULT NULL,
  `replace_rear_gear_line_price` decimal(6,2) DEFAULT NULL,
  
  -- Headset prices
  `repack_headset_price` decimal(6,2) DEFAULT NULL,
  `repack_headset_ebike_price` decimal(6,2) DEFAULT NULL,
  
  -- Rotor prices
  `replace_front_rotor_price` decimal(6,2) DEFAULT NULL,
  `replace_rear_rotor_price` decimal(6,2) DEFAULT NULL,
  
  -- E-bike and assembly prices
  `ebike_diagnostic_price` decimal(6,2) DEFAULT NULL,
  `bike_assembly_price` decimal(10,2) DEFAULT NULL,
  `ebike_assembly_price` decimal(10,2) DEFAULT NULL,
  
  -- Spoke repair prices (calculated: $35 base + $2 per spoke)
  `front_fix_spoke_price` decimal(10,2) DEFAULT NULL,
  `rear_fix_spoke_price` decimal(10,2) DEFAULT NULL,
  
  -- Custom service price
  `custom_service_price` decimal(10,2) DEFAULT NULL,
  
  -- Total pricing (subtotal and final with 8.75% NYC tax)
  `price` decimal(6,2) DEFAULT NULL,
  `final_price` decimal(10,2) DEFAULT NULL,
  
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
