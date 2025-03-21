// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import "@openzeppelin/contracts/access/Ownable2Step.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";

contract LTAIPaymentProcessor is Ownable2Step {
    IERC20 public immutable token;

    address public recipient;
    uint256 public burnPercentage; // burn percentage, between 0 and 100 (e.g., 80 means 80%)

    event PaymentProcessed(address indexed sender, uint256 amount, uint256 amountBurned, uint256 amountSent);
    event BurnPercentageUpdated(uint256 newBurnPercentage);
    event RecipientUpdated(address newRecipient);

    constructor(address _token, address _recipient, uint256 _initialBurnPercentage) Ownable(msg.sender) {
        require(_token != address(0), "Invalid token address");
        require(_recipient != address(0), "Invalid recipient address");
        require(_initialBurnPercentage < 101, "Invalid burn percentage");

        token = IERC20(_token);
        recipient = _recipient;
        burnPercentage = _initialBurnPercentage;
    }

    function processPayment(uint256 amount) external {
        require(amount != 0, "Amount must be greater than 0");

        address contractAddress = address(this);
        require(token.allowance(msg.sender, contractAddress) >= amount, "Insufficient allowance");

        uint256 amountToBurn = (amount * burnPercentage) / 100;
        uint256 amountToSend = amount - amountToBurn;

        require(token.transferFrom(msg.sender, contractAddress, amount), "Transfer failed");
        ERC20Burnable(address(token)).burn(amountToBurn);
        require(token.transfer(recipient, amountToSend), "Transfer to recipient failed");

        emit PaymentProcessed(msg.sender, amount, amountToBurn, amountToSend);
    }

    function setBurnPercentage(uint256 _newBurnPercentage) external onlyOwner payable {
        require(_newBurnPercentage < 101, "Invalid burn percentage");
        burnPercentage = _newBurnPercentage;
        emit BurnPercentageUpdated(_newBurnPercentage);
    }

    function setRecipient(address _newRecipient) external onlyOwner payable {
        require(_newRecipient != address(0), "Invalid recipient address");
        recipient = _newRecipient;
        emit RecipientUpdated(_newRecipient);
    }
}