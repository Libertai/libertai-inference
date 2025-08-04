import { Program } from "@coral-xyz/anchor";
import { Connection, PublicKey } from "@solana/web3.js";
import { program } from "..";
import idl from "../../target/idl/libertai_payment_processor.json";
import { LibertaiPaymentProcessor } from "../../target/types/libertai_payment_processor";

const getAdmins = async (
  anchorProgram: Program<LibertaiPaymentProcessor>
) => {
  const [programStatePDA] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_state")],
    anchorProgram.programId
  );

  const programState = await anchorProgram.account.programState.fetch(programStatePDA);

  console.log(programState.admins.length > 0 ? "Admins:" : "There are no admins except the owner");
  programState.admins.forEach((admin, index) => {
    console.log(`${index + 1}: ${admin.toString()}`);
  });
};

export const GetAdminsCommand = async () => {
  const opts = program.opts();

  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const anchorProgram = new Program<LibertaiPaymentProcessor>(idl, {
    connection,
  });

  await getAdmins(anchorProgram);
};
