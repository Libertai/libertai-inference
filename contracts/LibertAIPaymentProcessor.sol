// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/access/Ownable2Step.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import "@uniswap/v3-core/contracts/interfaces/callback/IUniswapV3SwapCallback.sol";

interface ISwapRouter02 is IUniswapV3SwapCallback {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    /// @notice Swaps `amountIn` of one token for as much as possible of another along the specified path
    /// @dev Setting `amountIn` to 0 will cause the contract to look up its own balance,
    /// and swap the entire amount, enabling contracts to send tokens before calling this function.
    /// @param params The parameters necessary for the multi-hop swap, encoded as `ExactInputParams` in calldata
    /// @return amountOut The amount of the received token
    function exactInput(
        ExactInputParams calldata params
    ) external payable returns (uint256 amountOut);
}

/**
 * @title LibertAIPaymentProcessor
 * @dev Contract for handling LibertAI payments with a burn mechanism and Uniswap integration
 * It processes payments received directly in LTAI tokens, burning a percentage and sending the rest to a recipient (team wallet)
 * Also supports receiving USDC (payments through external providers) and converting it to LTAI through Uniswap V3 to get a similar result than stated previously.
 */
contract LibertAIPaymentProcessor is Ownable2Step, AccessControl {
    // Token contracts
    IERC20 public immutable LTAI;
    IERC20 public immutable USDC;
    IERC20 public immutable WETH;

    // Uniswap settings
    ISwapRouter02 public immutable uniswapRouter;
    uint24 public wethLtaiPoolFee; // Pool fee for WETH/LTAI pair on Uniswap V3
    uint24 public usdcWethPoolFee; // Pool fee for USDC/WETH pair on Uniswap V3

    // Payment settings
    address public recipient; // Address that receives the non-burned portion of payments
    uint256 public burnPercentage; // Percentage of tokens to burn (0-100)

    // @notice Role allowed to process balances
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");

    /**
     * @dev Emitted when a payment is processed
     * @param sender The address initiating the payment
     * @param amount The total amount of LTAI tokens processed
     * @param amountBurned The amount of LTAI tokens burned
     * @param amountSent The amount of LTAI tokens sent to the recipient
     */
    event PaymentProcessed(
        address indexed sender,
        uint256 amount,
        uint256 amountBurned,
        uint256 amountSent
    );

    // Events for parameter updates
    event WethLtaiPoolFeeUpdated(uint24 newWethLtaiPoolFee);
    event UsdcWethPoolFeeUpdated(uint24 newUsdcWethPoolFee);
    event BurnPercentageUpdated(uint256 newBurnPercentage);
    event RecipientUpdated(address newRecipient);

    /**
     * @dev Sets up the payment processor with initial configuration
     * @param _ltaiAddress Address of the LTAI token contract
     * @param _usdcAddress Address of the USDC token contract
     * @param _wethAddress Address of the WETH token contract
     * @param _recipient Address to receive the non-burned portion of payments
     * @param _initialBurnPercentage Percentage of tokens to burn (0-100)
     * @param _uniswapRouter Address of the Uniswap V3 router
     * @param _wethLtaiPoolFee Fee tier for the WETH/LTAI pool
     * @param _usdcWethPoolFee Fee tier for the USDC/WETH pool
     */
    constructor(
        address _ltaiAddress,
        address _usdcAddress,
        address _wethAddress,
        address _recipient,
        uint256 _initialBurnPercentage,
        address _uniswapRouter,
        uint24 _wethLtaiPoolFee,
        uint24 _usdcWethPoolFee
    ) Ownable(msg.sender) {
        require(_ltaiAddress != address(0), "Invalid token address");
        require(_recipient != address(0), "Invalid recipient address");
        require(_initialBurnPercentage < 101, "Invalid burn percentage");

        LTAI = IERC20(_ltaiAddress);
        USDC = IERC20(_usdcAddress);
        WETH = IERC20(_wethAddress);
        recipient = _recipient;
        burnPercentage = _initialBurnPercentage;
        uniswapRouter = ISwapRouter02(_uniswapRouter);
        wethLtaiPoolFee = _wethLtaiPoolFee;
        usdcWethPoolFee = _usdcWethPoolFee;

        _grantRole(ADMIN_ROLE, msg.sender);
    }

    /**
     * @dev Processes a payment in LTAI tokens
     * @param amount The amount of LTAI tokens to process
     *
     * This function:
     * 1. Transfers LTAI tokens from the sender to this contract
     * 2. Burns a percentage of tokens based on the burnPercentage
     * 3. Sends the remaining tokens to the recipient address
     */
    function processPayment(uint256 amount) public {
        uint256 _amount = amount;
        address contractAddress = address(this);

        require(_amount != 0, "Amount must be greater than 0");

        // Case when it's called by an external sender, checking allowance
        require(
            LTAI.allowance(msg.sender, contractAddress) >= _amount,
            "Insufficient allowance"
        );

        // Calculate burn and send amounts based on the burn percentage
        uint256 amountToBurn = (_amount * burnPercentage) / 100;
        uint256 amountToSend = _amount - amountToBurn;

        // Transfer tokens from sender to this contract
        require(
            LTAI.transferFrom(msg.sender, contractAddress, _amount),
            "Transfer failed"
        );

        // Burn the specified percentage of tokens
        ERC20Burnable(address(LTAI)).burn(amountToBurn);

        // Send the remaining tokens to the recipient
        require(
            LTAI.transfer(recipient, amountToSend),
            "Transfer to recipient failed"
        );

        emit PaymentProcessed(msg.sender, _amount, amountToBurn, amountToSend);
    }

    /**
     * @dev Processes the USDC balance by swapping it for LTAI via Uniswap and then performing the burn/send mechanism
     * @param usdcAmount The amount of USDC in the balance to process
     * @param ltaiAmountMinimum The minimum amount of LTAI the swap should give
     *
     * This function:
     * 1. Swaps USDC → WETH → LTAI using Uniswap V3
     * 2. Processes the resulting LTAI tokens through the burn/send flow
     * Only callable by admins
     */
    function processUSDCBalance(
        uint256 usdcAmount,
        uint256 ltaiAmountMinimum
    ) external payable onlyRole(ADMIN_ROLE) {
        uint256 _usdcAmount = usdcAmount;
        uint256 _ltaiAmountMinimum = ltaiAmountMinimum;

        require(_usdcAmount != 0, "USDC amount must be >0");
        require(
            USDC.balanceOf(address(this)) >= _usdcAmount,
            "Not enough USDC"
        );

        // Approve the router to spend USDC.
        USDC.approve(address(uniswapRouter), _usdcAmount);

        // Set up the swap parameters for Uniswap (USDC → WETH → LTAI)
        ISwapRouter02.ExactInputParams memory params = ISwapRouter02
            .ExactInputParams({
                path: abi.encodePacked(
                    address(USDC),
                    uint24(usdcWethPoolFee),
                    address(WETH),
                    uint24(wethLtaiPoolFee),
                    address(LTAI)
                ),
                recipient: address(this),
                amountIn: _usdcAmount,
                amountOutMinimum: _ltaiAmountMinimum
            });

        // Execute the swap and get the amount of LTAI received
        uint256 ltaiReceived = uniswapRouter.exactInput(params);

        // Calculate burn and send amounts based on the burn percentage
        uint256 amountToBurn = (ltaiReceived * burnPercentage) / 100;
        uint256 amountToSend = ltaiReceived - amountToBurn;

        ERC20Burnable(address(LTAI)).burn(amountToBurn);
        require(
            LTAI.transfer(recipient, amountToSend),
            "Transfer to recipient failed"
        );
    }

    /**
     * @dev Processes the ETH balance by swapping it for LTAI via Uniswap and then performing the burn/send mechanism
     * @param ethAmount The amount of ETH in the balance to process
     * @param ltaiAmountMinimum The minimum amount of LTAI the swap should give
     *
     * This function:
     * 1. Swaps WETH → LTAI using Uniswap V3
     * 2. Processes the resulting LTAI tokens through the burn/send flow
     * Only callable by admins
     */
    function processETHBalance(
        uint256 ethAmount,
        uint256 ltaiAmountMinimum
    ) external payable onlyRole(ADMIN_ROLE) {
        uint256 _ethAmount = ethAmount;
        uint256 _ltaiAmountMinimum = ltaiAmountMinimum;

        require(_ethAmount != 0, "ETH amount must be >0");
        require(address(this).balance >= _ethAmount, "Not enough ETH");

        // Set up the swap parameters for Uniswap (USDC → WETH → LTAI)
        ISwapRouter02.ExactInputParams memory params = ISwapRouter02
            .ExactInputParams({
                path: abi.encodePacked(
                    address(WETH),
                    uint24(wethLtaiPoolFee),
                    address(LTAI)
                ),
                recipient: address(this),
                amountIn: _ethAmount,
                amountOutMinimum: _ltaiAmountMinimum
            });

        // Execute the swap and get the amount of LTAI received
        uint256 ltaiReceived = uniswapRouter.exactInput(params);

        // Calculate burn and send amounts based on the burn percentage
        uint256 amountToBurn = (ltaiReceived * burnPercentage) / 100;
        uint256 amountToSend = ltaiReceived - amountToBurn;

        ERC20Burnable(address(LTAI)).burn(amountToBurn);
        require(
            LTAI.transfer(recipient, amountToSend),
            "Transfer to recipient failed"
        );
    }

    /**
     * @dev Allows an admin to withdraw any ERC20 token (excluding USDC) to a specified address
     * @param token The ERC20 token to withdraw
     * @param to The address to send the tokens to
     * @param amount The amount of tokens to withdraw
     * Only callable by admins
     */
    function processERC20Balance(
        IERC20 token,
        address to,
        uint256 amount
    ) external onlyRole(ADMIN_ROLE) {
        require(address(token) != address(USDC), "Cannot withdraw USDC");
        require(to != address(0), "Invalid recipient address");
        require(amount > 0, "Amount must be greater than 0");
        require(
            token.balanceOf(address(this)) >= amount,
            "Insufficient token balance"
        );

        require(token.transfer(to, amount), "Token transfer failed");
    }

    /**
     * @dev Fallback function to receive ETH
     * This allows the contract to receive ETH payments directly
     */
    receive() external payable {}

    /**
     * @dev Updates the fee tier for the WETH/LTAI Uniswap pool
     * @param _newWethLtaiPoolFee The new fee tier (in hundredths of a percent)
     * Only callable by the contract owner
     */
    function setWethLtaiPoolFee(
        uint24 _newWethLtaiPoolFee
    ) external payable onlyOwner {
        wethLtaiPoolFee = _newWethLtaiPoolFee;
        emit WethLtaiPoolFeeUpdated(_newWethLtaiPoolFee);
    }

    /**
     * @dev Updates the fee tier for the USDC/WETH Uniswap pool
     * @param _newUsdcWethPoolFee The new fee tier (in hundredths of a percent)
     * Only callable by the contract owner
     */
    function setUsdcWethPoolFee(
        uint24 _newUsdcWethPoolFee
    ) external payable onlyOwner {
        usdcWethPoolFee = _newUsdcWethPoolFee;
        emit UsdcWethPoolFeeUpdated(_newUsdcWethPoolFee);
    }

    /**
     * @dev Updates the percentage of tokens to burn for each payment
     * @param _newBurnPercentage The new burn percentage (0-100)
     * Only callable by the contract owner
     */
    function setBurnPercentage(
        uint256 _newBurnPercentage
    ) external payable onlyOwner {
        require(_newBurnPercentage < 101, "Invalid burn percentage");
        burnPercentage = _newBurnPercentage;
        emit BurnPercentageUpdated(_newBurnPercentage);
    }

    /**
     * @dev Updates the recipient address that receives the non-burned portion of payments
     * @param _newRecipient The new recipient address
     * Only callable by the contract owner
     */
    function setRecipient(address _newRecipient) external payable onlyOwner {
        require(_newRecipient != address(0), "Invalid recipient address");
        recipient = _newRecipient;
        emit RecipientUpdated(_newRecipient);
    }

    /**
     * @dev Add a new admin
     * @param newAdmin The new admin address
     * Only callable by the contract owner
     */
    function addAdmin(address newAdmin) external onlyOwner {
        _grantRole(ADMIN_ROLE, newAdmin);
    }

    /**
     * @dev Remove an admin
     * @param admin The new admin address
     * Only callable by the contract owner
     */
    function removeAdmin(address admin) external onlyOwner {
        _revokeRole(ADMIN_ROLE, admin);
    }
}
